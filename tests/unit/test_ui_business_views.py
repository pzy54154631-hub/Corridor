"""UI projections and explicit gestures use the existing frozen business paths."""

from copy import deepcopy
from unittest.mock import MagicMock

import pytest

from corridor.crypto.jcs import H
from corridor.ui import business_views
from corridor.ui.controller import DemoController


@pytest.fixture
def controller():
    value = DemoController()
    value.start_session()
    return value


def test_subscription_projection_requires_approved_query_and_uses_receipt(controller):
    preview = controller.preview_subscriptions()
    assert business_views.subscription_projection(preview) == {"rows": [], "renewals": []}
    result = controller.approve_pending(preview["pending_id"])
    assert result["status"] == "settled"
    original = deepcopy(result)
    state_hash, checkpoint = H(controller.state), controller.gateway.audit.checkpoint()
    projected = business_views.subscription_projection(result)
    assert len(projected["rows"]) == 2
    assert projected["renewals"] == [
        {"authorization_ref": "S1", "next_due": "2026-10-19T02:00:00Z"}
    ]
    assert {row["amount"] for row in projected["rows"]} == {"49/1", "29/1"}
    assert result == original
    assert H(controller.state) == state_hash
    assert controller.gateway.audit.checkpoint() == checkpoint


def test_subscription_render_has_table_and_renewal_notice(controller, monkeypatch):
    pending = controller.preview_subscriptions()
    result = controller.approve_pending(pending["pending_id"])
    display = MagicMock()
    monkeypatch.setattr(business_views, "st", display)
    business_views.render_subscription_result(result, controller)
    assert display.dataframe.called
    assert "即将续费" in str(display.info.call_args)
    assert "2026-10-19 10:00:00" in str(display.info.call_args)
    assert "北京时间" in str(display.info.call_args)
    # Merchants now show their readable synthetic display name (UI-POLISH P2), not the
    # shared "synthetic recurring service" alias that made SUB_A and SUB_B identical.
    assert "会员服务A（合成）" in str(display.dataframe.call_args)
    assert "会员服务B（合成）" in str(display.dataframe.call_args)
    assert not display.json.called


def test_beijing_time_preserves_instant_and_handles_absent_receipt_time():
    assert business_views.beijing_time("2026-10-17T16:00:00Z") == "2026-10-18 00:00:00"
    assert business_views.beijing_time(None) == "—"


def test_remark_notice_is_display_only():
    assert business_views.remark_notice("转账备注妈妈，附言生日快乐")
    assert business_views.remark_notice("备注是妈妈")
    assert business_views.remark_notice("附言生日快乐")
    assert business_views.remark_notice("查询余额") is None


def test_business_messages_are_bilingual_and_remark_detection_is_locale_stable():
    from corridor.i18n import reset_locale, set_locale
    from corridor.ui.messages import UI_MESSAGES

    assert UI_MESSAGES["Session intent log"] == {
        "en": "Session intent log",
        "zh-CN": "会话侧意图日志",
    }
    token = set_locale("en")
    try:
        assert business_views.remark_notice("附言生日快乐").startswith("Contact remarks")
    finally:
        reset_locale(token)


def test_transaction_query_uses_green_gateway_path(controller):
    before = H(controller.state.accounts)
    result = business_views.query_transactions(controller)
    assert result["status"] == "settled"
    assert result["receipt"]["kind"] == "transactions_query"
    rows = business_views.transaction_projection(result)
    assert rows and all(row["account_ref"] == "A_CURRENT" for row in rows)
    assert H(controller.state.accounts) == before
    assert not controller.state.security.approval_requests
    assert not controller.state.security.consumed_grant_ids


def test_income_query_uses_green_gateway_path(controller):
    from corridor.sim.fixtures import make_state

    state = make_state()
    if not state.positions:
        from corridor.schemas.domain import Position

        state = state.model_copy(
            update={
                "positions": {
                    "POS_UI": Position(
                        id="POS_UI",
                        version=1,
                        owner=controller.device.principal,
                        product_ref="P_A",
                        principal_amount="1000/1",
                        units="1000/1",
                        reserved_units="0/1",
                        nav="1/1",
                        acquired_at="2026-09-01T02:00:00Z",
                        redeemable_at="2026-09-02T02:00:00Z",
                        status="active",
                    )
                }
            }
        )
    other = DemoController(initial_state=state)
    other.start_session()
    position = next(iter(state.positions))
    before = H(other.state.positions)
    result = business_views.query_income(other, position)
    assert result["status"] == "settled"
    assert result["analysis_receipt"]["function_id"] == "investment_income_v1"
    assert "realized_income" in result["analysis_receipt"]["results"]
    assert H(other.state.positions) == before
    assert not other.state.security.approval_requests
    assert not other.state.security.consumed_grant_ids


def test_timeline_is_ordered_read_only_and_keeps_intent_history_separate(controller):
    from corridor.agent.domestic_session import DomesticSession

    session = DomesticSession(controller)
    session.receive("查余额")
    audit = controller.gateway.audit.records()
    state_hash, checkpoint = H(controller.state), controller.gateway.audit.checkpoint()
    result = business_views.timeline_projection(audit, session.history)
    assert [row["seq"] for row in result["execution"]] == [row["seq"] for row in audit]
    assert result["intent"] == session.history
    assert result["intent"] is not session.history
    assert all("utterance_hash" not in row for row in result["execution"])
    assert any(row["status"] == "Accept" for row in result["execution"])
    assert H(controller.state) == state_hash
    assert controller.gateway.audit.checkpoint() == checkpoint


def test_aa_collection_needs_confirmation_and_creates_requests_without_debit(controller):
    before = H(controller.state.accounts)
    pending = business_views.preview_aa_collection(
        controller, "100", ["CONTACT_CHEN", "CONTACT_LIN_A", "CONTACT_LIN_B"]
    )
    assert pending["status"] == "awaiting_approval"
    assert not controller.state.receipts
    result = controller.approve_pending(pending["pending_id"])
    assert result["status"] == "settled"
    fields = result["analysis_receipt"]["results"]
    assert fields["total"]["value"] == "100/1"
    assert len(fields["request_ids"]["value"]) == 3
    assert H(controller.state.accounts) == before
    assert controller.gateway.bank.execute_count == 0


@pytest.mark.parametrize(
    "amount,participants",
    [
        ("0", ["CONTACT_CHEN"]),
        ("0.001", ["CONTACT_CHEN"]),
        ("1/0", ["CONTACT_CHEN"]),
        ("10", []),
        ("10", ["UNTRUSTED"]),
    ],
)
def test_aa_input_validation_does_not_create_authority(controller, amount, participants):
    before = H(controller.state)
    with pytest.raises(ValueError):
        business_views.preview_aa_collection(controller, amount, participants)
    assert H(controller.state) == before


def test_chat_subscription_query_renders_table_only_after_confirmation(monkeypatch):
    from tests.e2e.test_demo_ui import app, navigate

    monkeypatch.setenv("CORRIDOR_LOCALE", "zh-CN")
    page = app()
    page.button(key="enter").click().run()
    navigate(page, "im")
    page.text_input(key="im_text").set_value("我都订了什么会员")
    page.button(key="im_send").click().run()
    controller = page.session_state["controller"]
    assert not controller.state.receipts
    page.button(key="im_confirm").click().run()
    assert not page.exception
    assert page.dataframe
    assert any("即将续费" in item.value for item in page.info)
    assert controller.gateway.bank.execute_count == 0


def test_chat_remarks_notice_does_not_approve_transfer(monkeypatch):
    from tests.e2e.test_demo_ui import app, navigate

    monkeypatch.setenv("CORRIDOR_LOCALE", "zh-CN")
    page = app()
    page.button(key="enter").click().run()
    navigate(page, "im")
    page.text_input(key="im_text").set_value("给Chen转200元，附言生日快乐")
    page.button(key="im_send").click().run()
    assert not page.exception
    assert any("附言不会随转账发送" in item.value for item in page.info)
    assert page.session_state["controller"].gateway.bank.execute_count == 0


def test_chat_red_transfer_requires_typed_simulated_code(monkeypatch):
    from tests.e2e.test_demo_ui import app, navigate

    monkeypatch.setenv("CORRIDOR_LOCALE", "zh-CN")
    page = app()
    page.button(key="enter").click().run()
    navigate(page, "im")
    page.text_input(key="im_text").set_value("给Chen转1500元")
    page.button(key="im_send").click().run()
    page.button(key="im_confirm").click().run()
    assert not page.exception
    page.button(key="im_verify_mfa:request").click().run()
    controller = page.session_state["controller"]
    session = page.session_state["domestic_session"]
    response = session.response(page.session_state["im_response_id"])
    pending_id = response["payload"]["pending_id"]
    pending = controller._pending[pending_id]
    assert not pending["verified"]
    assert controller.gateway.bank.execute_count == 0
    page.text_input(key="im_verify_mfa:code").set_value(
        controller.gateway.mfa.test_code(pending["challenge_id"])
    )
    page.button(key="im_verify_mfa:verify").click().run()
    assert not page.exception
    assert controller._pending[pending_id]["verified"]
    assert controller.gateway.bank.execute_count == 0
    page.button(key="im_confirm").click().run()
    assert not page.exception
    assert controller.gateway.bank.execute_count == 1


def test_billing_transactions_income_and_audit_controls(monkeypatch):
    from tests.e2e.test_demo_ui import app, navigate

    monkeypatch.setenv("CORRIDOR_LOCALE", "zh-CN")
    page = app()
    page.button(key="enter").click().run()
    navigate(page, "billing")
    page.button(key="query_transactions").click().run()
    assert not page.exception
    assert page.session_state["transaction_query_result"]["status"] == "settled"
    navigate(page, "wealth")
    page.button(key="query_income").click().run()
    assert not page.exception
    assert page.session_state["income_query_result"]["result"]["status"] == "settled"
    navigate(page, "audit")
    assert not page.exception
    assert any("意图路由不在哈希链内" in item.value for item in page.info)
    assert any(item.value == "执行时间线" for item in page.subheader)


def test_aa_collection_ui_requires_confirm_and_creates_exact_shares(monkeypatch):
    from tests.e2e.test_demo_ui import app, navigate

    monkeypatch.setenv("CORRIDOR_LOCALE", "zh-CN")
    page = app()
    page.button(key="enter").click().run()
    navigate(page, "transfers")
    page.multiselect(key="aa_participants").set_value(["CONTACT_CHEN", "CONTACT_LIN_A"])
    page.button(key="aa_preview").click().run()
    controller = page.session_state["controller"]
    assert not page.exception
    assert not controller.state.user_inputs
    page.button(key="aa_confirm").click().run()
    assert not page.exception
    result = page.session_state["aa_result"]
    assert result["status"] == "settled"
    assert result["analysis_receipt"]["results"]["shares.CONTACT_CHEN"]["value"] == "50/1"
    assert controller.gateway.bank.execute_count == 0
