"""S1: the chat page shows the history table and the interrupt receipt it was given."""

from tests.e2e.test_demo_ui import app


def _send(page, text):
    page.text_input(key="domestic_text").set_value(text)
    page.button(key="domestic_send").click().run()
    assert not page.exception


def test_chat_history_request_renders_the_billing_pages_table(monkeypatch):
    monkeypatch.setenv("CORRIDOR_LOCALE", "zh-CN")
    page = app()
    page.button(key="enter").click().run()
    _send(page, "查一下我最近的交易记录")
    session = page.session_state["domestic_session"]
    response = session.response(page.session_state["domestic_response_id"])
    assert response["payload"]["receipt"]["kind"] == "transactions_query"
    assert page.dataframe
    assert not page.session_state["controller"].state.user_inputs


def test_chat_interrupt_shows_executed_and_cancelled_nodes(monkeypatch):
    monkeypatch.setenv("CORRIDOR_LOCALE", "zh-CN")
    page = app()
    page.button(key="enter").click().run()
    session = page.session_state["domestic_session"]
    draft = session.receive("妈妈生日安排一下")
    session.confirm(
        draft["response_id"], selected_node_ids=[node["id"] for node in draft["payload"]["nodes"]]
    )
    _send(page, "停一下，先别执行了")
    response = session.response(page.session_state["domestic_response_id"])
    assert response["status"] == "interrupted"
    assert "已执行：锁定预算" in response["message"]
    shown = [item.value for item in page.markdown]
    assert response["message"] in shown, shown
