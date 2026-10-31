"""Isolated birthday demonstrations retain real frozen-gateway decisions."""

from copy import deepcopy

import pytest

from corridor.crypto.jcs import H
from corridor.ui.chain_demo import (
    create_chain_probe,
    create_normal_birthday,
    finish_normal_birthday,
    preview_chain_change,
    run_chain_probe,
)
from corridor.ui.controller import DemoController


@pytest.mark.parametrize("kind", ["subscription", "business_day", "destination", "balance"])
def test_change_prediction_is_read_only_and_dispatch_uses_frozen_gateway(kind):
    active = DemoController()
    active.start_session()
    original = H(active.state)
    demo = create_chain_probe(kind)
    case = demo["case"]
    before = H(case.state)
    count = case.gateway.bank.execute_count
    audit = deepcopy(case.gateway.audit.records())
    prediction = preview_chain_change(demo)
    assert prediction["affected_node_ids"]
    assert H(case.state) == before
    assert case.gateway.audit.records() == audit
    assert case.gateway.bank.execute_count == count
    result = run_chain_probe(demo)
    assert result["status"] == "rejected"
    assert result["target_bank_calls"] == 0
    assert result["kernel_outcome"] == "Reject"
    assert result["changed_components"]
    assert result["reference_label"] == "参考判定 · 研究口径"
    assert result["reference_holds"] is (kind == "subscription")
    assert result["token_consumed"]
    assert result["reject_streak_after"] > result["reject_streak_before"]
    assert case.gateway.audit.verify()
    assert H(active.state) == original
    finished = H(case.state)
    assert run_chain_probe(demo) == result
    assert H(case.state) == finished


def test_subscription_is_native_debit_and_changes_only_financial_dependency():
    demo = create_chain_probe("subscription")
    result = run_chain_probe(demo)
    assert result["available_before"] == "9000/1"
    assert result["available_after"] == "8951/1"
    assert result["changed_components"] == ["financial"]
    assert any(
        receipt.kind == "subscription_debit" for receipt in demo["case"].state.receipts.values()
    )
    assert result["classification"] == "虚假失效（参考判定）"


def test_cross_day_does_not_confuse_expired_approval_with_day_change():
    demo = create_chain_probe("business_day")
    result = run_chain_probe(demo)
    assert result["changed_components"] == ["business_date"]
    assert demo["case"].state.now < demo["token"].expiry
    assert result["classification"] == "必要失效（参考判定）"


def test_balance_probe_has_real_money_predicate_and_zero_target_calls():
    demo = create_chain_probe("balance")
    result = run_chain_probe(demo)
    assert demo["prepared"].action.kind == "lock_funds"
    assert "BAL-AVAILABLE" in result["kernel_rules"]
    assert result["available_after"] == "100/1"


def test_no_change_control_dispatches_the_same_held_approval_once():
    demo = create_chain_probe("none")
    result = run_chain_probe(demo)
    assert result["status"] == "controlled"
    assert result["kernel_outcome"] == "Accept"
    assert result["changed_components"] == []
    assert result["reference_holds"]
    assert result["classification"] == "正常成功"


def test_probe_comparison_reports_removed_and_added_dependency_keys(monkeypatch):
    from corridor.ui import chain_demo

    demo = create_chain_probe("none")
    original = chain_demo.dependency_hashes

    def changed_keys(*args):
        hashes = original(*args)
        hashes.pop("policy")
        hashes["display_added"] = "added"
        return hashes

    monkeypatch.setattr(chain_demo, "dependency_hashes", changed_keys)
    result = run_chain_probe(demo)
    assert result["changed_components"] == ["display_added", "policy"]
    assert result["status"] == "controlled"


def test_normal_birthday_explicit_setup_completes_without_operator_or_fault():
    demo = create_normal_birthday()
    controller = demo["controller"]
    assert demo["setup_result"]["status"] == "settled"
    assert len(controller.state.schedules) == 3
    assert len(controller.state.orders) == 0
    approvals = len(controller.state.security.consumed_nonces)
    result = finish_normal_birthday(demo)
    assert result["complete"]
    assert result["paid_orders"] == 2
    assert controller.gateway.bank.execute_count == 4
    assert len(controller.state.security.consumed_nonces) == approvals == 4
    assert all(run.status == "executed" for run in controller.state.schedules.values())
    assert all(order.status == "paid" for order in controller.state.orders.values())
    assert controller.gateway.verify_log()
    assert finish_normal_birthday(demo) == result


def test_unknown_probe_is_rejected_before_creating_authority():
    with pytest.raises(ValueError, match="Unknown chain demonstration"):
        create_chain_probe("bad")


def test_birthday_ui_requires_prediction_then_dispatch_and_retains_both_outcomes():
    from tests.e2e.test_demo_ui import app, navigate

    page = app()
    page.button(key="enter").click().run()
    navigate(page, "birthday")
    controller = page.session_state["controller"]
    original = H(controller.state)
    for kind in CHANGE_NAMES_FOR_TEST:
        page.button(key=f"chain_probe_{kind}").click().run()
        assert not page.exception
        probe = page.session_state["chain_probe"]
        assert "result" not in probe
        assert any("影响预告" in message.value for message in page.info)
        page.button(key="chain_probe_dispatch").click().run()
        assert not page.exception
        assert probe["result"]["target_bank_calls"] == 0
    assert H(controller.state) == original
    assert any("三个应失效探针" in message.value for message in page.success)
    assert any("有些良性变化" in message.value for message in page.warning)


CHANGE_NAMES_FOR_TEST = ("subscription", "business_day", "destination", "balance")


def test_normal_birthday_ui_finishes_with_two_explicit_gestures_without_operator_mode():
    from tests.e2e.test_demo_ui import app, navigate

    page = app()
    page.button(key="enter").click().run()
    navigate(page, "birthday")
    active = page.session_state["controller"]
    original = H(active.state)
    scope = " ".join(message.value for message in page.info)
    assert all(value in scope for value in ("1,000", "500", "300", "200", "FLOWERS", "CAKE"))
    assert "2026-10-17 16:00 UTC" in scope and "2026-10-18 16:00 UTC" in scope
    assert "一次点击，脚本化逐张签署四张规范卡" in page.button(key="birthday_normal_start").label
    page.button(key="birthday_normal_start").click().run()
    assert not page.exception
    demo = page.session_state["birthday_normal"]
    assert not demo["controller"].state.orders
    page.button(key="birthday_normal_finish").click().run()
    assert not page.exception
    assert demo["completion"]["complete"]
    assert any("生日链正常完成" in message.value for message in page.success)
    assert H(active.state) == original


def test_normal_scope_is_derived_from_unsigned_canonical_cards():
    from corridor.ui.chain_demo import normal_birthday_scope, preview_normal_birthday

    demo = preview_normal_birthday()
    controller, draft = demo["controller"], demo["draft"]
    before = H(controller.state), controller.gateway.audit.checkpoint()
    draft["nodes"][2]["card"]["amount"] = "321/1"
    draft["nodes"][2]["card"]["execution_window"]["start"] = "2026-10-18T15:00:00Z"
    scope = normal_birthday_scope(draft)
    assert "321.00" in scope and "2026-10-18 15:00 UTC" in scope
    assert "300.00" not in scope
    assert not controller.state.schedules and not controller.state.orders
    assert before == (H(controller.state), controller.gateway.audit.checkpoint())


def test_password_card_hides_payment_facts_and_plan_anchors_are_scoped(monkeypatch):
    from corridor.ui import components

    markdown = []
    monkeypatch.setattr(components.st, "markdown", lambda value, **kwargs: markdown.append(value))
    monkeypatch.setattr(components.st, "warning", lambda *args, **kwargs: None)
    from contextlib import nullcontext

    monkeypatch.setattr(components.st, "expander", lambda *args, **kwargs: nullcontext())
    monkeypatch.setattr(components.st, "json", lambda *args, **kwargs: None)
    controller = DemoController()
    controller.start_session()
    canonical = controller.preview_password_change()["card"]
    assert canonical["kind"] == "password_change_request"
    assert "fees" in canonical and "total_after" in canonical
    components.render_card(canonical, controller=controller)
    card = markdown[0]
    from corridor.ui.messages import tr

    assert tr("Fees") not in card and tr("Daily total · after") not in card
    keys = []
    monkeypatch.setattr(
        components, "render_dag", lambda snapshot, **kwargs: keys.append(kwargs["key"])
    )
    components.render_plans([{"plan": {"plan_id": "a"}}], key="active")
    components.render_plans([{"plan": {"plan_id": "a"}}], key="isolated")
    assert len(set(keys)) == 2
