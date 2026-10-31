"""Domestic channels share unsigned previews and explicit device gestures."""

import pytest

from corridor.agent.domestic_session import DomesticSession
from corridor.crypto.jcs import H
from corridor.ui.controller import DemoController


def session():
    controller = DemoController()
    controller.start_session()
    return DomesticSession(controller)


def test_chat_transfer_has_no_authority_until_bound_confirmation_and_no_repay():
    chat = session()
    before = H(chat.controller.state)
    response = chat.receive("给Chen转200元", request_id="one")
    assert response["route"] == "transfers"
    assert response["card"]["tier"] == "yellow"
    assert H(chat.controller.state) == before
    assert chat.controller.gateway.bank.execute_count == 0
    result = chat.confirm(response["response_id"])
    assert result["status"] == "settled"
    assert chat.confirm(response["response_id"])["status"] == "settled"
    assert chat.controller.gateway.bank.execute_count == 1
    with pytest.raises(ValueError, match="Request ID"):
        chat.receive("给Chen转900元", request_id="one")


@pytest.mark.parametrize("text", ["讲个笑话", "明天天气怎么样", "推荐一只股票", "写一首生日诗"])
def test_out_of_scope_calls_neither_model_nor_business_tools(monkeypatch, text):
    chat = session()
    before = H(chat.controller.state)
    audit = chat.controller.gateway.audit.checkpoint()

    def forbidden(*args, **kwargs):
        raise AssertionError("Out-of-scope request reached a tool")

    monkeypatch.setattr(chat.client, "complete", forbidden)
    response = chat.receive(text)
    assert response["status"] == "refused"
    assert response["intent"] == "out_of_scope"
    assert response["rule_ids"] == []
    assert H(chat.controller.state) == before
    assert chat.controller.gateway.audit.checkpoint() == audit


def test_sensitive_card_requires_separate_intent_mfa_and_final_confirmation():
    from corridor.bench.domestic_runner import make_session

    chat = make_session("card")
    before = H(chat.controller.state)
    response = chat.receive("卡丢了，赶紧挂失")
    identifier = response["response_id"]
    assert response["tier"] == "red"
    assert H(chat.controller.state) == before
    assert chat.verify_mfa(identifier)["status"] == "intent_confirmation_required"
    assert chat.confirm(identifier)["status"] == "waiting_mfa"
    assert chat.controller.gateway.bank.execute_count == 0
    assert chat.verify_mfa(identifier)["status"] == "mfa_verified"
    assert chat.confirm(identifier)["status"] == "settled"
    assert chat.controller.state.cards["CARD_DEMO"].status == "lost"
    assert chat.controller.state.security.consumed_grant_ids


def test_retired_response_cannot_execute_after_stop():
    chat = session()
    response = chat.receive("给Chen转200元")
    chat.receive("停")
    assert chat.confirm(response["response_id"])["status"] == "withdrawn"
    assert chat.controller.gateway.bank.execute_count == 0


def test_stop_in_another_channel_retires_old_confirmation():
    chat = session()
    response = chat.receive("给Chen转200元")
    chat.controller.stop()
    assert chat.confirm(response["response_id"])["status"] == "withdrawn"
    assert chat.controller.gateway.bank.execute_count == 0


def test_model_cannot_supply_executable_values_and_strict_miss_cannot_fallback(monkeypatch):
    from types import SimpleNamespace

    from corridor.agent.llm import ReplayMiss

    chat = session()
    before = H(chat.controller.state)
    monkeypatch.setattr(
        chat.client,
        "complete",
        lambda **kwargs: SimpleNamespace(
            output={"intent": "transfer", "needs_clarification": False, "amount": "9000/1"}
        ),
    )
    with pytest.raises(ValueError):
        chat.receive("给Chen转200元")
    assert H(chat.controller.state) == before

    def missing(**kwargs):
        raise ReplayMiss("Synthetic missing Chinese intent replay")

    monkeypatch.setattr(chat.client, "complete", missing)
    with pytest.raises(ReplayMiss):
        chat.receive("给Chen转200元")
    assert H(chat.controller.state) == before
    assert chat.controller.gateway.bank.execute_count == 0


def test_wealth_request_never_uses_signed_purchase_preview(monkeypatch):
    chat = session()

    def forbidden(*args, **kwargs):
        raise AssertionError("Chat must not sign a purchase")

    monkeypatch.setattr(chat.controller, "preview_investment", forbidden)
    monkeypatch.setattr(chat.controller, "compare_investments", forbidden)
    before = H(chat.controller.state)
    response = chat.receive("有五万闲钱想理财")
    assert response["status"] == "awaiting_device_selection"
    assert response["route"] == "wealth"
    assert response["tier"] is None
    assert H(chat.controller.state) == before


def test_im_keeps_the_same_response_binding_and_never_auto_approves():
    from corridor.channels.im import IMAdapter

    chat = session()
    adapter = IMAdapter(chat)
    message = adapter.receive("给Chen转200元")
    assert message["simulated"]
    assert message["buttons"][0]["response_id"] == message["response_id"]
    assert chat.controller.gateway.bank.execute_count == 0
    result = adapter.click("confirm", message["response_id"])
    assert result["status"] == "settled"


@pytest.mark.parametrize("text,control", [("停", "stopped"), ("转人工", "handoff")])
@pytest.mark.parametrize("bank_draft", [False, True])
def test_chat_control_retires_wealth_authority_and_preserves_settled_purchases(
    text, control, bank_draft
):
    from tests.unit.test_ui_wealth_controller import prepared_controller

    wealth = prepared_controller()
    overview = wealth.prepare_allocation()
    wealth.start_allocation([item["item_ref"] for item in overview["items"]])
    first = wealth.leg["action"].action_id
    assert wealth.verify_leg(first)["status"] == "mfa_verified"
    assert wealth.approve_leg(first)["status"] == "settled"
    second = wealth.leg["action"].action_id
    assert wealth.verify_leg(second)["status"] == "mfa_verified"
    old_leg = wealth.leg.copy()
    old_token = wealth.device.approve(
        old_leg["action"], old_leg["cert"], old_leg["card"], now=wealth.state.now, sequence=1
    )
    positions = H(wealth.state.positions)
    chat = session()
    chat.wealth_controller = wealth
    transfer = chat.receive("给Chen转200元") if bank_draft else None
    response = chat.receive(text)

    if transfer:
        assert chat.confirm(transfer["response_id"])["status"] == "withdrawn"
    assert chat.controller.gateway.bank.execute_count == 0
    assert wealth.comparison is None and wealth.draft is None and wealth.leg is None
    execution = wealth.execution_snapshot()
    assert execution["status"] == "partially_executed"
    assert execution["control_status"] == control
    assert response["payload"]["wealth"]["execution"] == execution
    payload = response["payload"]
    if not bank_draft or control == "handoff":
        assert payload["bank"]["status"] == "no_active_plan"
        assert response["status"] == execution["status"]
    else:
        assert payload["bank"]["status"] == response["status"] == "interrupted"
    assert payload["audit_checkpoints"] == {
        "bank": chat.controller.gateway.audit.checkpoint(),
        "wealth": wealth.gateway.audit.checkpoint(),
    }
    actual_rules = set(payload["wealth"].get("rule_ids", [])) | set(execution["rule_ids"])
    for result in payload["wealth"].get("results", {}).values():
        actual_rules.update(result.get("rule_ids", []))
    if payload["wealth"].get("handoff"):
        actual_rules.update(payload["wealth"]["handoff"].get("rule_ids", []))
    actual_rules.update(payload["bank"].get("rule_ids", []))
    assert response["rule_ids"] == sorted(actual_rules)
    if control == "handoff":
        assert bool(response["payload"]["session_snapshot"]) == bank_draft
        assert response["route"] == "operator"
    stale = wealth.gateway.dispatch(old_leg["action"], old_leg["cert"], old_token)
    assert stale.status == "rejected"
    assert ("PLAN-OWNER" if control == "handoff" else "APR-VOID") in stale.rule_ids
    assert wealth.gateway.bank.execute_count == 1
    assert H(wealth.state.positions) == positions


@pytest.mark.parametrize("text", ["停", "转人工"])
def test_chat_control_also_withdraws_unsigned_wealth_comparison(text):
    from tests.unit.test_ui_wealth_controller import prepared_controller

    wealth = prepared_controller()
    wealth.prepare_allocation()
    chat = session()
    chat.wealth_controller = wealth
    chat.receive(text)
    assert wealth.comparison is None and wealth.draft is None and wealth.candidate is None
    assert not wealth.state.allocation_plans
    assert wealth.gateway.bank.execute_count == 0


def test_handoff_snapshot_reports_current_outcomes_and_retired_drafts():
    chat = session()
    settled = chat.receive("给Chen转200元")
    assert chat.confirm(settled["response_id"])["status"] == "settled"
    draft = chat.receive("给Chen转100元")
    handed_off = chat.receive("转人工")
    statuses = {
        row["response_id"]: row["status"] for row in handed_off["payload"]["session_snapshot"]
    }
    assert statuses[settled["response_id"]] == "settled"
    assert statuses[draft["response_id"]] == "withdrawn"
    assert chat.controller.gateway.bank.execute_count == 1
