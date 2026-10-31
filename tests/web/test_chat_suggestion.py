"""Model suggestions in the web chat: shown as system-written questions, sent as messages.

Scripted, the page shows none and the route has nothing to choose. Live (the test-only
fake model answers the suggestion step), the clarification lists the canonical sentence
as a question with the fixed note; choosing it sends exactly that sentence as a message
(the browser sends only ``c<i>`` and the list version), the normal card follows and only
the device gesture approves it. A stale list, a replayed click and a sentence the rules
no longer read are refused without sending anything; a send that fails adopts nothing and
leaves the choice open.
"""

import pytest

import corridor.web
from corridor.agent import domestic_suggest
from corridor.agent.domestic_intent import IntentCandidate
from corridor.agent.llm import LLMOutputError
from corridor.crypto.jcs import jcs_bytes
from corridor.web import contracts
from tests.suggest_fakes import FREEZE, SENTENCE, TRANSFER, UNCLEAR, suggest_rule
from tests.unit.test_ui_live_mode import FakeModel, environ
from tests.unit.test_ui_scam_friction import Spy
from tests.web.conftest import ws_of
from tests.web.test_api_chat import Browser
from tests.web.test_api_live import (
    browser,
    current_task,
    data,
    gesture,
    go_live,
    send,
    state_of,
)

LIVE = corridor.web.FEATURES | {"live"}
QUESTION = "您是想：给妈妈转500元吗？"
NOTE = "建议由模型生成，点击后仍需按卡逐步确认。"


@pytest.fixture
def web_settings(monkeypatch):
    # The routes read the app's features and the view models read the package's: both open.
    monkeypatch.setattr(corridor.web, "FEATURES", LIVE)
    return {"features": LIVE}


@pytest.fixture
def hosted(monkeypatch, tmp_path):
    def apply(**extra):
        for name, value in environ(tmp_path, **extra).items():
            monkeypatch.setenv(name, value)
        return tmp_path / "journal"

    return apply


def choose(client, rkey, *, choice="c0", version=None, message="suggest-0001"):
    shown = state_of(client)["chat"]["responses"][rkey]["suggestion_select"]
    body = {
        "choice": choice,
        "list_version": version or shown["list_version"],
        "client_msg_id": message,
    }
    return client.post(f"/api/chat/responses/{rkey}/suggestion", body)


def live_reply(web_app, monkeypatch, hosted, *items, view="judge"):
    FakeModel(monkeypatch, rule=suggest_rule(*items))
    hosted()
    client = browser(web_app, view=view)
    go_live(client)
    body = data(client, send(client, UNCLEAR))
    return client, ws_of(web_app, client), body


# --- scripted ------------------------------------------------------------------------------


@pytest.mark.parametrize("view", ["customer", "judge"])
def test_the_scripted_chat_shows_no_suggestion_and_has_nothing_to_choose(web_app, view):
    page = Browser(web_app, view=view)
    page.login()
    rkey = page.send(UNCLEAR)
    vm = page.response(rkey)
    assert vm["status"] == "clarify" and "suggestion_select" not in vm
    contracts.validate_state(page.state)
    assert page.ws.session.suggestions.enabled is True
    assert page.ws.session.suggestions.log.records() == []
    page.fail(
        f"/api/chat/responses/{rkey}/suggestion",
        {"choice": "c0", "list_version": "0" * 32, "client_msg_id": "suggest-0001"},
        "not_found",
        status=404,
    )


# --- live ------------------------------------------------------------------------------------


@pytest.mark.parametrize("view", ["customer", "judge"])
def test_a_live_clarification_lists_template_questions_and_prepares_nothing(
    web_app, hosted, monkeypatch, view
):
    client, ws, body = live_reply(web_app, monkeypatch, hosted, TRANSFER, FREEZE, view=view)
    contracts.validate_response(body, view=view)
    vm = body["state"]["chat"]["responses"][body["rkey"]]
    shown = vm["suggestion_select"]
    assert [item["label"] for item in shown["choices"]] == [QUESTION, "您是想：冻结我的银行卡吗？"]
    assert [item["state"] for item in shown["choices"]] == ["offered", "offered"]
    assert shown["note"] == NOTE and shown["locked"] is False
    assert vm["status"] == "clarify" and vm["cards"] == [] and vm["device_task"] is None
    assert ("evidence_lines" in shown) is (view == "judge")
    assert ws.controller.gateway.bank.execute_count == 0
    assert ws.controller._drafts == {} and ws.controller._pending == {}


def test_choosing_a_suggestion_sends_its_sentence_and_only_the_device_approves(
    web_app, hosted, monkeypatch
):
    client, ws, body = live_reply(web_app, monkeypatch, hosted, TRANSFER)
    listed = body["rkey"]
    spy = Spy(ws.controller, monkeypatch)
    response = choose(client, listed)
    answer = data(client, response)
    assert response.code == 200, answer
    contracts.validate_response(answer)
    chosen = answer["rkey"]
    state = answer["state"]
    assert chosen != listed and state["chat"]["current"] == chosen
    user = [turn for turn in state["chat"]["turns"] if turn["role"] == "user"]
    assert user[-1]["text"] == SENTENCE
    prepared = ws.session.response(ws.rkeys[chosen])
    assert prepared["card"]["kind"] == "transfer" and prepared["status"] == "awaiting_input"
    assert prepared["card"]["objects"]["contact_ref"] == "CONTACT_CHEN"
    old = state["chat"]["responses"][listed]["suggestion_select"]
    assert old["choices"][0]["state"] == "adopted" and old["locked"] is True
    assert spy.counts["approve"] == spy.counts["dispatch"] == 0
    assert ws.controller.gateway.bank.execute_count == 0
    # The device gesture is the step that approves, as for any typed transfer.
    task = current_task(client, chosen)
    assert task["step"] == "confirm"
    assert gesture(client, task).code == 200
    assert ws.session.response(ws.rkeys[chosen])["status"] == "settled"
    assert ws.controller.gateway.bank.execute_count == 1
    records = ws.session.suggestions.log.records()
    assert [record["event"] for record in records] == [
        "suggestion_generated",
        "suggestion_adopted",
    ]
    assert records[-1]["payload"]["adopted_response_id"] == ws.rkeys[chosen]


def test_choosing_equals_typing_the_sentence(web_app, hosted, monkeypatch):
    chooser, chooser_ws, first = live_reply(web_app, monkeypatch, hosted, TRANSFER)
    typist = browser(web_app)
    go_live(typist)
    typist_ws = ws_of(web_app, typist)
    other = data(typist, send(typist, UNCLEAR))
    assert jcs_bytes(chooser_ws.session.response(chooser_ws.rkeys[first["rkey"]])) == jcs_bytes(
        typist_ws.session.response(typist_ws.rkeys[other["rkey"]])
    )
    chosen = data(chooser, choose(chooser, first["rkey"]))["rkey"]
    typed = data(typist, send(typist, SENTENCE))["rkey"]
    assert jcs_bytes(chooser_ws.session.response(chooser_ws.rkeys[chosen])) == jcs_bytes(
        typist_ws.session.response(typist_ws.rkeys[typed])
    )
    assert chooser_ws.transcript[-2].hint == typist_ws.transcript[-2].hint == SENTENCE
    assert (
        chooser_ws.controller.gateway.audit.records()
        == typist_ws.controller.gateway.audit.records()
    )


def test_a_replayed_click_returns_its_reply_and_a_stale_list_sends_nothing(
    web_app, hosted, monkeypatch
):
    client, ws, body = live_reply(web_app, monkeypatch, hosted, TRANSFER, FREEZE)
    listed = body["rkey"]
    shown = state_of(client)["chat"]["responses"][listed]["suggestion_select"]
    stale = choose(client, listed, version="f" * 32)
    assert stale.code == 409 and data(client, stale)["error"]["code"] == "card_changed"
    assert len(ws.session.history) == 1
    first = data(client, choose(client, listed, version=shown["list_version"]))["rkey"]
    again = choose(client, listed, version=shown["list_version"])
    assert again.code == 200 and data(client, again)["rkey"] == first
    assert len(ws.session.history) == 2
    # The other suggestion of the earlier reply has expired: nothing is sent.
    late = choose(client, listed, choice="c1", message="suggest-0002")
    assert late.code == 409 and data(client, late)["error"]["code"] == "not_current"
    client.post(f"/api/chat/responses/{listed}/current", {})
    expired = state_of(client)["chat"]["responses"][listed]["suggestion_select"]
    assert [item["state"] for item in expired["choices"]] == ["adopted", "expired"]
    late = choose(client, listed, choice="c1", message="suggest-0003")
    assert late.code == 409 and data(client, late)["error"]["code"] == "not_applicable"
    assert len(ws.session.history) == 2


def test_a_sentence_the_rules_no_longer_read_is_discarded_with_a_notice(
    web_app, hosted, monkeypatch
):
    """The desk checks the sentence again and discards it: nothing is sent, and the list
    says so. The desk is the route's first counted product call (it marked the suggestion
    and wrote its log), so the refusal that follows leaves ``Write.run`` as an unknown
    result and the session is distrusted (RW-5 item 2); the notice still shows."""
    client, ws, body = live_reply(web_app, monkeypatch, hosted, TRANSFER)
    monkeypatch.setattr(
        domestic_suggest,
        "recognize",
        lambda text, context=None: IntentCandidate(intent="clarify", needs_clarification=True),
    )
    response = choose(client, body["rkey"])
    answer = data(client, response)
    assert response.code == 500 and answer["error"]["code"] == "outcome_unknown"
    assert ws.untrusted is True
    shown = answer["state"]["chat"]["responses"][body["rkey"]]["suggestion_select"]
    assert shown["choices"][0]["state"] == "discarded" and shown["locked"] is True
    assert shown["choices"][0]["state_text"] == (
        "这条建议已失效：系统规则已无法识别这句话，没有发送任何内容。"
    )
    assert [record["event"] for record in ws.session.suggestions.log.records()] == [
        "suggestion_generated",
        "suggestion_discarded",
    ]
    assert len(ws.session.history) == 1 and ws.controller.gateway.bank.execute_count == 0


def test_a_failed_send_adopts_nothing_and_the_suggestion_stays_offered(
    web_app, hosted, monkeypatch
):
    """Both reviews: a suggestion is adopted only once its sentence reached the session. A
    send that fails (here the live model refuses the intent step) answers 422, logs the
    failed send, shows no "sent" text and leaves the same choice open for a retry."""
    fake = FakeModel(monkeypatch, rule=suggest_rule(TRANSFER))
    hosted()
    client = browser(web_app)
    go_live(client)
    body = data(client, send(client, UNCLEAR))
    ws = ws_of(web_app, client)
    listed = body["rkey"]
    working = fake.rule

    def refusing(step, output):
        if str(step).startswith("intent:"):
            return LLMOutputError("Invalid model response; no replay recorded")
        return working(step, output)

    fake.rule = refusing
    failed = choose(client, listed)
    answer = data(client, failed)
    assert failed.code == 422 and answer["error"]["code"] == "operation_failed"
    shown = answer["state"]["chat"]["responses"][listed]["suggestion_select"]
    assert shown["choices"][0]["state"] == "offered" and shown["choices"][0]["state_text"] is None
    assert shown["locked"] is False and answer["state"]["chat"]["current"] == listed
    assert [record["event"] for record in ws.session.suggestions.log.records()] == [
        "suggestion_generated",
        "suggestion_send_failed",
    ]
    assert any("send_failed:model_output" in line for line in shown["evidence_lines"])
    assert len(ws.session.history) == 1 and ws.controller.gateway.bank.execute_count == 0
    # The model answers again: the same choice is sent, and only now adopted.
    fake.rule = working
    again = choose(client, listed, message="suggest-0002")
    chosen = data(client, again)
    assert again.code == 200, chosen
    old = chosen["state"]["chat"]["responses"][listed]["suggestion_select"]
    assert old["choices"][0]["state"] == "adopted"
    assert ws.session.suggestions.log.records()[-1]["event"] == "suggestion_adopted"
    assert ws.session.response(ws.rkeys[chosen["rkey"]])["card"]["kind"] == "transfer"


def test_a_failed_model_call_keeps_the_plain_clarification(web_app, hosted, monkeypatch):
    def broken(step, output):
        if str(step).startswith("suggest:"):
            return {"suggestions": [TRANSFER], "reply": "我已经帮您转好了"}
        return output

    FakeModel(monkeypatch, rule=broken)
    hosted()
    client = browser(web_app)
    go_live(client)
    body = data(client, send(client, UNCLEAR))
    vm = body["state"]["chat"]["responses"][body["rkey"]]
    assert vm["status"] == "clarify" and "suggestion_select" not in vm
    ws = ws_of(web_app, client)
    assert "suggestions" not in ws.session.response(ws.rkeys[body["rkey"]])["payload"]
    assert [record["payload"]["reason"] for record in ws.session.suggestions.log.records()] == [
        "schema"
    ]
    assert "我已经帮您转好了" not in str(body)
