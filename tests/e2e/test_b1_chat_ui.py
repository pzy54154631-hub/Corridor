"""B1: the chat page finishes a subscription cancellation, a purchase and a redemption.

Every card still comes from the business form's own preparer; the page's buttons are the
device gestures. Rendered in the customer view (the default) and checked for the facts.
"""

from tests.e2e.test_demo_ui import app


def _send(page, text):
    page.text_input(key="domestic_text").set_value(text)
    page.button(key="domestic_send").click().run()
    assert not page.exception


def _texts(page):
    return " ".join(
        str(item.value)
        for group in (page.markdown, page.caption, page.info, page.warning)
        for item in group
    )


def _current(page):
    session = page.session_state["domestic_session"]
    return session, session.response(page.session_state["domestic_response_id"])


def _enter(monkeypatch):
    monkeypatch.setenv("CORRIDOR_LOCALE", "zh-CN")
    page = app()
    page.button(key="enter").click().run()
    return page


def test_naming_the_amount_shows_the_yellow_cancellation_card_and_confirm_cancels(monkeypatch):
    page = _enter(monkeypatch)
    _send(page, "把每月扣49块的那个会员取消掉")
    session, response = _current(page)
    assert response["card"]["kind"] == "cancel_subscription"
    assert "取消订阅" in _texts(page)
    page.button(key="domestic_confirm").click().run()
    assert not page.exception
    controller = page.session_state["controller"]
    assert controller.state.subscriptions["S1"].status == "cancelled"
    assert session.response(response["response_id"])["status"] == "settled"


def test_an_unnamed_cancellation_lists_choices_and_a_choice_prepares_the_card(monkeypatch):
    page = _enter(monkeypatch)
    _send(page, "取消订阅")
    session, listed = _current(page)
    assert listed["payload"]["subscription_choices"] == ["S1"]
    assert "请选择要取消的代扣授权" in _texts(page)
    page.button(key=f"domestic_subscription:{listed['response_id']}:S1").click().run()
    assert not page.exception
    _, chosen = _current(page)
    assert chosen["response_id"] != listed["response_id"]
    assert chosen["card"]["objects"]["authorization_ref"] == "S1"
    assert not page.session_state["controller"].state.user_inputs


def test_a_complete_purchase_is_reviewed_then_confirm_prepares_the_red_card(monkeypatch):
    page = _enter(monkeypatch)
    _send(page, "帮我申购2000元稳健30天持有")
    session, review = _current(page)
    assert review["payload"]["purchase"]["product_ref"] == "P_A"
    texts = _texts(page)
    assert "申购要素（待您在模拟设备上确认，确认后才生成审批卡）" in texts
    assert (
        "¥2,000.00" in " ".join(str(item.value) for item in page.markdown) or "¥2,000.00" in texts
    )
    controller = page.session_state["controller"]
    assert not controller.state.user_inputs
    page.button(key="domestic_confirm").click().run()
    assert not page.exception
    current = session.response(review["response_id"])
    assert current["status"] == "waiting_mfa"
    assert current["card"]["kind"] == "purchase_product"
    assert controller.gateway.bank.execute_count == 0


def test_a_purchase_missing_its_product_asks_in_chat(monkeypatch):
    page = _enter(monkeypatch)
    _send(page, "买两万元理财")
    session, response = _current(page)
    assert response["payload"]["purchase_parse"]["missing"] == ["product"]
    texts = _texts(page)
    assert "您想申购哪一款" in texts and "稳健30天持有" in texts
    # The page's session (with the wealth page's dialogue attached) continues the slots.
    # A second form submission in one AppTest page keeps a stale input (see S6.md §6).
    review = session.receive("稳健30天持有那款")
    assert review["payload"]["purchase"]["amount"] == "20000/1"
    assert review["payload"]["purchase"]["product_ref"] == "P_A"


def test_redeeming_the_showcase_holding_shows_the_redemption_card(monkeypatch):
    # C1 (review of B1): the one holding no longer stands in for the quantity; the request
    # names it (全部). Without a quantity the chat asks (test_c1_dialogue_review).
    page = _enter(monkeypatch)
    _send(page, "把我持有的那笔理财全部赎回")
    session, response = _current(page)
    assert response["card"]["kind"] == "redeem_product"
    page.button(key="domestic_confirm").click().run()
    assert not page.exception
    assert session.response(response["response_id"])["status"] == "settled"
