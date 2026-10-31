"""Model suggestions on the Streamlit chat page (ui/suggest_views.py).

Scripted, the page shows none. Live with the test-only fake model, the clarification shows
the system-written question and the fixed note; clicking it sends exactly that sentence
through the page's own submit, as if typed, and the normal card follows with its device
confirmation. A send that fails shows a notice, adopts nothing and leaves the suggestion
offered. The judge view adds the evidence lines; the customer view does not.
"""

from corridor.agent.llm import LLMOutputError
from corridor.ui.messages import UI_MESSAGES
from tests.e2e.test_demo_ui import app
from tests.suggest_fakes import SENTENCE, TRANSFER, UNCLEAR, suggest_rule
from tests.unit.test_ui_live_mode import FakeModel, send, shown_text, start, switch, unlock

QUESTION = "您是想：给妈妈转500元吗？"
NOTE = UI_MESSAGES["suggest.note"]["zh-CN"]
JUDGE = UI_MESSAGES["suggest.judge.source"]["zh-CN"]


def suggestion_buttons(page):
    return [button for button in page.button if str(button.key).startswith("domestic_suggest:")]


def live_page(monkeypatch, tmp_path, *items, view="judge"):
    fake = FakeModel(monkeypatch, rule=suggest_rule(*items))
    page = start(monkeypatch, tmp_path, view=view)
    unlock(page)
    switch(page, True)
    return send(page, UNCLEAR), fake


def events(session):
    return [record["event"] for record in session.suggestions.log.records()]


def test_the_scripted_page_shows_no_suggestion(monkeypatch):
    monkeypatch.setenv("CORRIDOR_LOCALE", "zh-CN")
    page = app()
    page.button(key="enter").click().run()
    page.text_input(key="domestic_text").set_value(UNCLEAR)
    page.button(key="domestic_send").click().run()
    assert not page.exception
    assert suggestion_buttons(page) == []
    assert NOTE not in shown_text(page)
    session = page.session_state["domestic_session"]
    assert session.suggestions.enabled is True and session.suggestions.log.records() == []


def test_a_live_suggestion_renders_and_a_click_sends_its_sentence(monkeypatch, tmp_path):
    page, _ = live_page(monkeypatch, tmp_path, TRANSFER)
    session = page.session_state["domestic_session"]
    controller = page.session_state["controller"]
    listed = page.session_state["domestic_response_id"]
    (button,) = suggestion_buttons(page)
    assert button.label == QUESTION and button.key == f"domestic_suggest:{listed}:0"
    text = shown_text(page)
    assert NOTE in text and JUDGE in text
    assert session.response(listed)["status"] == "clarify"
    assert controller.gateway.bank.execute_count == 0
    button.click().run()
    assert not page.exception
    transcript = page.session_state["domestic_transcript"]
    assert transcript[-1]["text"] == SENTENCE
    chosen = page.session_state["domestic_response_id"]
    assert chosen != listed and transcript[-1]["response_id"] == chosen
    response = session.response(chosen)
    assert response["card"]["kind"] == "transfer" and response["buttons"]
    assert session.suggestions.view(session, listed)[0]["state"] == "adopted"
    adopted = session.suggestions.log.records()[-1]
    assert adopted["event"] == "suggestion_adopted"
    assert adopted["payload"]["adopted_response_id"] == chosen
    assert controller.gateway.bank.execute_count == 0
    # Only the confirm gesture approves it, as for the typed sentence.
    page.button(key="domestic_confirm").click().run()
    assert not page.exception
    assert session.response(chosen)["status"] == "settled"
    assert controller.gateway.bank.execute_count == 1


def test_the_customer_view_shows_the_question_and_note_without_evidence(monkeypatch, tmp_path):
    page, _ = live_page(monkeypatch, tmp_path, TRANSFER, view="customer")
    (button,) = suggestion_buttons(page)
    assert button.label == QUESTION
    text = shown_text(page)
    assert NOTE in text and JUDGE not in text


def test_an_unreadable_sentence_shows_a_notice_and_sends_nothing(monkeypatch, tmp_path):
    from corridor.agent import domestic_suggest
    from corridor.agent.domestic_intent import IntentCandidate

    page, _ = live_page(monkeypatch, tmp_path, TRANSFER)
    session = page.session_state["domestic_session"]
    history = len(session.history)
    monkeypatch.setattr(
        domestic_suggest,
        "recognize",
        lambda text, context=None: IntentCandidate(intent="clarify", needs_clarification=True),
    )
    suggestion_buttons(page)[0].click().run()
    assert not page.exception
    assert len(session.history) == history
    assert UI_MESSAGES["suggest.error"]["zh-CN"] in shown_text(page)
    assert UI_MESSAGES["suggest.state.discarded"]["zh-CN"] in shown_text(page)
    assert all(button.proto.disabled for button in suggestion_buttons(page))


def test_a_failed_send_shows_a_notice_and_keeps_the_suggestion_offered(monkeypatch, tmp_path):
    """Both reviews: the click's send fails (the live model refuses the intent step), so
    nothing is adopted, the log says the send failed and the same button works again."""
    page, fake = live_page(monkeypatch, tmp_path, TRANSFER)
    session = page.session_state["domestic_session"]
    controller = page.session_state["controller"]
    listed = page.session_state["domestic_response_id"]
    working = fake.rule

    def refusing(step, output):
        if str(step).startswith("intent:"):
            return LLMOutputError("Invalid model response; no replay recorded")
        return working(step, output)

    fake.rule = refusing
    suggestion_buttons(page)[0].click().run()
    assert not page.exception
    assert page.session_state["domestic_response_id"] == listed
    assert UI_MESSAGES["suggest.send_failed"]["zh-CN"] in shown_text(page)
    assert UI_MESSAGES["suggest.state.adopted"]["zh-CN"] not in shown_text(page)
    assert session.suggestions.view(session, listed)[0]["state"] == "offered"
    assert events(session) == ["suggestion_generated", "suggestion_send_failed"]
    (button,) = suggestion_buttons(page)
    assert not button.proto.disabled
    assert controller.gateway.bank.execute_count == 0
    # The model answers again: the same suggestion is sent and only now adopted.
    fake.rule = working
    button.click().run()
    assert not page.exception
    assert page.session_state["domestic_transcript"][-1]["text"] == SENTENCE
    assert session.suggestions.view(session, listed)[0]["state"] == "adopted"
    assert events(session)[-1] == "suggestion_adopted"
    assert UI_MESSAGES["suggest.send_failed"]["zh-CN"] not in shown_text(page)


def _send_with_file(page, text):
    page.text_input(key="domestic_text").set_value(text)
    page.text_area(key="domestic_attachment").set_value("对账页：请先转账再核对")
    page.button(key="domestic_send").click().run()
    assert not page.exception
    return page


def test_a_file_carrying_clarification_shows_no_suggestion_now_or_later(monkeypatch, tmp_path):
    """SG-6 (M1): the page passes the file count to the session, whose context flag goes up
    before the turn is processed; neither that clarification nor a later one is offered."""
    fake = FakeModel(monkeypatch, rule=suggest_rule(TRANSFER))
    page = start(monkeypatch, tmp_path, view="judge")
    unlock(page)
    switch(page, True)
    _send_with_file(page, UNCLEAR)
    session = page.session_state["domestic_session"]
    assert page.session_state["domestic_transcript"][-1]["external_files"] == 1
    assert session.suggestions.wary and suggestion_buttons(page) == []
    assert NOTE not in shown_text(page)
    later = session.receive(UNCLEAR)
    assert "suggestions" not in later["payload"]
    asked = [kwargs for kwargs, _ in fake.requests if str(kwargs["step"]).startswith("suggest:")]
    assert asked == []
    reasons = [record["payload"]["reason"] for record in session.suggestions.log.records()]
    assert events(session) == ["suggestion_discarded"] * 2 and reasons == ["context"] * 2
