"""Topic B's visible device profile, signed goals, certificate and purchases."""

import pytest

from corridor.ui.messages import tr
from tests.e2e.test_demo_ui import app, navigate, phone_inbox_codes


def prepare_wealth(page):
    page.button(key="enter").click().run()
    navigate(page, "wealth")
    page.text_input(key="wealth_message").set_value("I want something safe")
    page.button(key="wealth_discuss").click().run()
    controller = page.session_state["wealth_controller"]
    assert controller.risk_profile() is None
    assert not controller.state.user_inputs
    navigate(page, "device")
    for index in range(5):
        page.selectbox(key=f"wealth_risk_{index}").select(1)
    page.button(key="wealth_sign_risk").click().run()
    assert not page.exception
    assert controller.risk_profile()["level"] == 2
    page.text_input(key="wealth_amount").set_value("10000")
    page.text_input(key="wealth_liquid").set_value("0")
    page.button(key="wealth_confirm_goals").click().run()
    assert not page.exception
    assert controller.comparison is not None
    assert not controller.state.allocation_plans
    navigate(page, "wealth")
    return controller


def type_leg_code(page, controller, key):
    """Request this leg's simulated SMS, then type the code the device panel shows.

    Replaces the removed auto-read ``wealth_verify`` button; approval stays disabled
    until the frozen MFAVerifier accepts the typed code for this leg only.
    """
    page.button(key=f"wealth_request:{key}").click().run()
    assert not page.exception
    assert not controller.leg["verified"]
    assert page.button(key=f"wealth_approve:{key}").disabled
    code = controller.gateway.mfa.test_code(controller.leg["challenge_id"])
    # Per-leg codes now reach the simulated phone page, not the main page (UI-POLISH P4).
    assert code not in [element.value for element in page.code]
    assert code in phone_inbox_codes(page)
    page.text_input(key=f"wealth_code:{key}").set_value(code)
    page.button(key=f"wealth_verify:{key}").click().run()
    assert not page.exception
    assert controller.leg["verified"]


def test_wealth_device_profile_goals_and_comparison_are_real():
    page = app()
    controller = prepare_wealth(page)
    assert not controller.state.positions
    assert controller.comparison.cert.allocation.totals.principal == 10000
    assert controller.gateway.verify_log()
    assert controller.comparison.result.status == "settled"
    assert controller.gateway.bank.execute_count == 0
    navigate(page, "wealth")
    assert not page.exception
    assert controller.wealth_chat.signed_request_ref in controller.state.user_inputs
    assert controller.risk_profile() is not None
    page.text_input(key="wealth_message").set_value("Actually, use a different amount")
    page.button(key="wealth_discuss").click().run()
    assert controller.comparison is None
    assert controller.wealth_chat.signed_request_ref is None
    assert controller.dialogue_result["proposal_withdrawn"]
    assert controller.gateway.bank.execute_count == 0


@pytest.mark.parametrize("locale", ["zh-CN", "en"])
def test_full_topic_b_route_two_separate_mfa_receipts_and_audit(monkeypatch, locale):
    monkeypatch.setenv("CORRIDOR_LOCALE", locale)
    page = app()
    controller = prepare_wealth(page)
    boxes = [box for box in page.checkbox if box.key.startswith("wealth_select:")]
    assert len(boxes) == 2 and all(not box.value for box in boxes)
    assert page.button(key="wealth_start").disabled
    boxes[0].check().run()
    assert page.button(key="wealth_start").disabled
    assert not controller.state.allocation_plans
    for box in page.checkbox:
        if box.key.startswith("wealth_select:"):
            box.check()
    page.run()
    page.button(key="wealth_start").click().run()
    assert not page.exception
    navigate(page, "device")
    assert any(tr("APR-BUDGET preflight · ") in item.value for item in page.caption)
    for number in range(2):
        key = controller.leg["action"].action_id
        assert page.button(key=f"wealth_approve:{key}").disabled
        type_leg_code(page, controller, key)
        assert not page.button(key=f"wealth_approve:{key}").disabled
        page.button(key=f"wealth_approve:{key}").click().run()
        assert not page.exception
        assert len(controller.state.positions) == number + 1
    page.run()
    assert controller.gateway.bank.execute_count == 2
    assert controller.execution_snapshot()["available_balance"] == "2000/1"
    assert len(controller.state.security.consumed_grant_ids) == 2
    assert not controller.state.security.locked_at
    navigate(page, "wealth")
    assert controller.execution_snapshot()["status"] == "completed"
    navigate(page, "audit")
    assert controller.gateway.verify_log()


def test_unknown_wealth_outcome_shows_lookup_but_no_approve():
    from corridor.kernel.binding import intent_key

    page = app()
    controller = prepare_wealth(page)
    for box in page.checkbox:
        if box.key.startswith("wealth_select:"):
            box.check()
    page.run()
    page.button(key="wealth_start").click().run()
    navigate(page, "device")
    key = controller.leg["action"].action_id
    controller.gateway.bank.faults[intent_key(controller.leg["action"], controller.state)] = (
        "response_loss"
    )
    type_leg_code(page, controller, key)
    page.button(key=f"wealth_approve:{key}").click().run()
    assert not page.exception
    assert controller.execution_snapshot()["status"] == "unknown"
    assert page.button(key="wealth_lookup")
    assert not [
        button for button in page.button if (button.key or "").startswith("wealth_approve:")
    ]
    page.button(key="wealth_lookup").click().run()
    assert controller.gateway.bank.execute_count == 1
    assert controller.leg is None


@pytest.mark.parametrize("locale", ["zh-CN", "en"])
def test_four_attack_buttons_run_real_isolated_cases_without_changing_wealth(monkeypatch, locale):
    from corridor.crypto.jcs import H
    from corridor.ui.evaluation import ATTACK_CLASSES

    monkeypatch.setenv("CORRIDOR_LOCALE", locale)
    page = app()
    page.button(key="enter").click().run()
    navigate(page, "wealth")
    controller = page.session_state["wealth_controller"]
    before = H(controller.state)
    for kind in ATTACK_CLASSES:
        page.button(key=f"wealth_attack:{kind}").click().run()
        assert not page.exception
        result = page.session_state["wealth_attack_result"]
        assert result["passed"] is (kind != "prompt_injection")
        assert result["benign_completed"]
        if kind == "prompt_injection":
            assert result["attack"]["completed"] is False
            assert result["attack"]["passed"] is False
            assert result["attack"]["attacker_induced_lockout"] is True
            assert result["attack"]["availability_cost"] == {
                "locked": True,
                "legitimate_task_refused": True,
            }
            assert any(
                item.value == tr("Contained by lockout (legitimate task refused)")
                for item in page.warning
            )
            assert any(
                item.value == tr("Availability cost: account locked and legitimate task refused.")
                for item in page.markdown
            )
            assert not any(tr("Blocked · ") in item.value for item in page.success)
        assert result["funds_moved_cny"] == "0/1"
        assert result["audit_entries"] and result["rule_ids"]
        assert result["evidence_mode"] == "scripted"
        assert H(controller.state) == before


def test_advisor_shows_continuous_slots_and_device_prefills_without_signing():
    page = app()
    page.button(key="enter").click().run()
    navigate(page, "wealth")
    page.text_input(key="wealth_message").set_value("I have 10000 to invest")
    page.button(key="wealth_discuss").click().run()
    controller = page.session_state["wealth_controller"]
    assert controller.wealth_chat.signed_request_ref is None
    assert controller.dialogue_result["pending_slots"]
    assert controller.dialogue_result["phase"] == "collecting"
    page.text_input(key="wealth_message").set_value("I need 2000 within 3 days")
    page.button(key="wealth_discuss").click().run()
    navigate(page, "device")
    assert page.text_input(key="wealth_amount").value == "10000.00"
    assert page.text_input(key="wealth_liquid").value == "2000.00"
    assert not controller.state.user_inputs
    assert not page.exception


def test_loss_signal_displays_gateway_cap_and_coaching_clarification():
    page = app()
    controller = prepare_wealth(page)
    navigate(page, "wealth")
    page.text_input(key="wealth_message").set_value("一分都不能亏")
    page.button(key="wealth_discuss").click().run()
    assert not page.exception
    assert controller.suitability_details["effective_level"] == 1
    assert controller.risk_profile()["level"] == 2
    page.text_input(key="wealth_message").set_value("教我怎么答")
    page.button(key="wealth_discuss").click().run()
    assert controller.dialogue_result["suitability_signal"]["signal"] == "coaching_request"
    assert controller.dialogue_result["suitability_signal"]["status"] == "refused"
    navigate(page, "device")
    assert controller.suitability_details["effective_level"] == 1
    assert controller.gateway.bank.execute_count == 0


def test_strict_replay_failure_is_never_labeled_as_guided(monkeypatch):
    from corridor.agent.llm import ReplayMiss

    page = app()
    page.button(key="enter").click().run()
    navigate(page, "wealth")
    controller = page.session_state["wealth_controller"]
    controller.client.mode = "strict"

    def missing(**kwargs):
        raise ReplayMiss("Synthetic missing replay")

    monkeypatch.setattr(controller.client, "complete", missing)
    page.text_input(key="wealth_message").set_value("I have 10000 to invest")
    page.button(key="wealth_discuss").click().run()
    assert page.error
    assert controller.dialogue_result is None
    assert not controller.guided
    assert not controller.state.user_inputs


@pytest.mark.parametrize("locale", ["zh-CN", "en"])
def test_questionnaire_order_matches_policy_and_loss_answer_caps_effective_level(
    monkeypatch, locale
):
    monkeypatch.setenv("CORRIDOR_LOCALE", locale)
    page = app()
    page.button(key="enter").click().run()
    controller = page.session_state["wealth_controller"]
    navigate(page, "device")
    for index in range(len(controller.state.policy.risk_questions)):
        page.selectbox(key=f"wealth_risk_{index}").select(0 if index == 1 else 4)
    page.button(key="wealth_sign_risk").click().run()
    assert not page.exception
    assert controller.risk_profile()["level"] == 4
    assert controller.risk_profile()["answers"][1] == 0
    assert controller.risk_profile()["answers"] == [4, 0, 4, 4, 4]
    assert controller.suitability_details["effective_level"] == 1
    assert any(tr("Effective risk level: C") + "1" in item.value for item in page.warning)


@pytest.mark.parametrize("locale", ["zh-CN", "en"])
def test_signed_goal_correction_displays_authenticated_read_only_difference(monkeypatch, locale):
    from corridor.crypto.jcs import H

    monkeypatch.setenv("CORRIDOR_LOCALE", locale)
    page = app()
    controller = prepare_wealth(page)
    navigate(page, "device")
    page.text_input(key="wealth_amount").set_value("9000")
    page.button(key="wealth_confirm_goals").click().run()
    assert not page.exception
    assert controller.comparison.cert.allocation.totals.principal == 9000
    expected = controller.wealth_chat.change_explanation()
    before = H(controller.state)
    navigate(page, "wealth")
    assert not page.exception
    assert any(
        tr("What changed after your signed correction") in item.value for item in page.subheader
    )
    table = page.dataframe[0].value
    principal_fact = next(
        fact for fact in expected if fact.product_id is None and fact.field == "principal"
    )
    principal = table[
        (table[tr("Product")] == tr("Allocation total"))
        & (table[tr("Fact")] == tr(principal_fact.label))
    ]
    assert principal.iloc[0][tr("Before (CNY)")] == "10000.00"
    assert principal.iloc[0][tr("After (CNY)")] == "9000.00"
    assert principal.iloc[0][tr("Change (CNY)")] == "-1000.00"
    assert any(fact.product_id == "P_A" and fact.exact_value == "-500/1" for fact in expected)
    assert H(controller.state) == before
    assert controller.gateway.bank.execute_count == 0
