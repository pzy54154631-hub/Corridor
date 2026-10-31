"""B2: the chat page answers a cancellation of a scheduled transfer in two statements, lists
the customer's own authorizations, and a choice prepares the revocation card in chat."""

from tests.e2e.test_demo_ui import app


def _texts(page):
    return " ".join(
        str(item.value)
        for group in (page.markdown, page.caption, page.info, page.warning)
        for item in group
    )


def test_cancelling_a_monthly_transfer_lists_it_and_the_choice_revokes_it(monkeypatch):
    monkeypatch.setenv("CORRIDOR_LOCALE", "zh-CN")
    page = app()
    page.button(key="enter").click().run()
    session = page.session_state["domestic_session"]
    card = session.receive("每月5号给房东转2000元")
    session.confirm(card["response_id"])
    session.verify_mfa(card["response_id"])
    assert session.confirm(card["response_id"])["status"] == "controlled"
    page.text_input(key="domestic_text").set_value("取消每月5号给房东的转账")
    page.button(key="domestic_send").click().run()
    assert not page.exception
    listed = session.response(page.session_state["domestic_response_id"])
    [spa] = listed["payload"]["schedule_cancel"]["candidates"]
    texts = _texts(page)
    assert "这条消息没有发起新付款" in texts and "已有的定时转账授权尚未取消" in texts
    page.button(key=f"domestic_schedule_cancel:{listed['response_id']}:{spa}").click().run()
    assert not page.exception
    chosen = session.response(page.session_state["domestic_response_id"])
    assert chosen["card"]["kind"] == "cancel_schedule"
    page.button(key="domestic_confirm").click().run()
    assert not page.exception
    assert session.response(chosen["response_id"])["status"] == "controlled"
    assert "已撤销这项定时转账授权的剩余执行" in _texts(page)
    controller = page.session_state["controller"]
    assert all(
        task.status == "cancelled"
        for task in controller.state.schedules.values()
        if task.spa_ref == spa
    )
