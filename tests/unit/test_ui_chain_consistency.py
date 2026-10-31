"""Read-only chain evidence never changes authorization or fabricates a verdict."""

from corridor.crypto.jcs import H
from corridor.stores.bank import add_seconds
from corridor.ui.chain_consistency import (
    affected_downstream,
    approval_preflight,
    chain_rows,
    compare_dependencies,
    reference_preconditions,
)
from corridor.ui.controller import DemoController


def test_budget_uses_frozen_spendable_limit_and_clock_release():
    controller = DemoController()
    controller.start_session()
    before = H(controller.state), controller.gateway.audit.checkpoint()
    budget = approval_preflight(controller, required=4)
    assert budget["used"] == 0 and budget["limit"] == 6 and budget["remaining"] == 5
    assert budget["window_seconds"] == 600 and budget["can_start"]
    assert before == (H(controller.state), controller.gateway.audit.checkpoint())
    draft = controller.preview_birthday()
    controller.confirm_plan(draft["plan_id"], [node["id"] for node in draft["nodes"]])
    budget = approval_preflight(controller, required=4)
    assert budget["used"] == 4 and budget["remaining"] == 1 and not budget["can_start"]
    controller.advance_clock(600)
    assert approval_preflight(controller, required=4)["can_start"]


def test_dependency_comparison_keeps_missing_and_changed_components():
    rows = compare_dependencies(
        {"policy": "a", "financial": "b"}, {"policy": "a", "financial": "c"}
    )
    assert [row["component"] for row in rows if row["changed"]] == ["financial"]
    assert all(row["changed"] is None for row in compare_dependencies(None, {"policy": "a"}))
    assert compare_dependencies({"objects": "x"}, {})[0]["changed"] is True


def test_propagation_uses_dependencies_without_marking_unrelated_nodes():
    nodes = [
        {"node_id": "a", "depends_on": []},
        {"node_id": "b", "depends_on": ["a"]},
        {"node_id": "c", "depends_on": ["b"]},
        {"node_id": "d", "depends_on": []},
    ]
    assert affected_downstream(nodes, {"a"}) == ["b", "c"]
    assert affected_downstream(nodes, {"b"}) == ["c"]


def test_draft_panel_is_unsigned_and_read_only():
    controller = DemoController()
    controller.start_session()
    draft = controller.preview_birthday()
    before = H(controller.state), controller.gateway.audit.checkpoint()
    rows = chain_rows(controller, draft)
    assert len(rows) == 4
    assert all(row["actual"]["status"] == "not_dispatched" for row in rows)
    assert all(row["reference"]["status"] == "not_evaluated" for row in rows)
    assert all(not row["signed_goal"] for row in rows)
    assert before == (H(controller.state), controller.gateway.audit.checkpoint())


def test_birthday_panel_excludes_unrelated_query_plans():
    controller = DemoController()
    controller.start_session()
    assert controller.bill_report()["status"] == "settled"
    draft = controller.preview_birthday()
    before = H(controller.state), controller.gateway.audit.checkpoint()
    rows = chain_rows(controller, draft)
    assert len(rows) == 4
    assert {row["plan_id"] for row in rows} == {draft["plan_id"]}
    assert {row["node_id"] for row in rows} == {node["id"] for node in draft["nodes"]}
    assert before == (H(controller.state), controller.gateway.audit.checkpoint())


def test_completed_panel_retains_actual_dispatch_and_token_consumption():
    controller = DemoController()
    controller.start_session()
    draft = controller.preview_birthday()
    plan = controller._plans[draft["plan_id"]]
    confirmation = controller.workflow.confirm_plan(
        controller.gateway,
        plan["display"].display_id,
        [node["id"] for node in draft["nodes"]],
        request_id=draft["plan_id"],
    )
    plan["confirmation"] = confirmation
    before = H(controller.state), controller.gateway.audit.checkpoint()
    rows = chain_rows(controller, draft)
    assert [row["actual"]["status"] for row in rows] == [
        "settled",
        "controlled",
        "controlled",
        "controlled",
    ]
    assert all(row["authorization"]["token"]["consumed"] for row in rows)
    assert all(row["signed_goal"] for row in rows)
    assert all(row["dependencies"] for row in rows)
    assert all(row["actual"]["kernel"] == "Accept" for row in rows)
    assert before == (H(controller.state), controller.gateway.audit.checkpoint())


def test_normal_chain_is_not_presented_as_drift_or_propagated_before_or_after_due():
    """A clean four-node run must not read as changed premises or affected nodes."""
    from corridor.ui.chain_consistency import consistency_html

    controller = DemoController()
    controller.start_session()
    draft = controller.preview_birthday()
    result = controller.confirm_plan(draft["plan_id"], [node["id"] for node in draft["nodes"]])
    assert result["status"] == "settled"
    for _ in range(2):
        before = H(controller.state), controller.gateway.audit.checkpoint()
        rows = chain_rows(controller, draft)
        assert len(rows) == 4
        assert all(row["downstream"] == [] for row in rows)
        assert [row["reference"]["status"] for row in rows] == ["holds"] * 4
        assert all(row["reference"]["at_dispatch"] for row in rows)
        output = consistency_html(rows)
        assert "所列前提改变" not in output and "按派发时状态" in output
        assert "按派发时状态 · 所列前提保持" in output
        assert "预订鲜花" in output and "预订蛋糕" in output
        assert (
            "depends_on:" not in output and "settled" not in output and "controlled" not in output
        )
        assert "依赖节点：2. 到期解锁" in output
        assert before == (H(controller.state), controller.gateway.audit.checkpoint())
        controller.advance_clock(max(run.due_at for run in controller.state.schedules.values()))


def test_rejected_proposal_is_not_an_approval_snapshot_or_dispatch_verdict():
    from corridor.ui.chain_consistency import consistency_html
    from corridor.ui.controller import demo_state

    state = demo_state()
    merchant = state.merchants["FLOWERS"].model_copy(update={"is_new": True})
    state = state.model_copy(update={"merchants": {**state.merchants, merchant.id: merchant}})
    controller = DemoController(initial_state=state)
    controller.start_session()
    draft = controller.preview_birthday()
    result = controller.confirm_plan(draft["plan_id"], [node["id"] for node in draft["nodes"]])
    assert result["status"] == "rejected" and result["rule_ids"] == ["DOMAIN-SPA"]
    before = H(controller.state), controller.gateway.audit.checkpoint()
    rows = chain_rows(controller, draft)
    assert all(row["authorization"]["token"] for row in rows[:2])
    flower = rows[2]
    assert flower["authorization"]["token"] is None
    assert flower["actual"]["status"] == "not_dispatched"
    assert flower["actual"]["kernel"] is None
    assert flower["proposal"] == {"outcome": "Reject", "rule_ids": ["DOMAIN-SPA"]}
    assert flower["dependencies"]
    assert all(
        item["approved"] is None and item["changed"] is None for item in flower["dependencies"]
    )
    assert flower["reference"]["status"] == "not_evaluated"
    output = consistency_html([flower])
    assert "尚无批准时快照" in output and "分量一致" not in output
    assert "提案检查（非派发）" in output and "DOMAIN-SPA" in output
    assert before == (H(controller.state), controller.gateway.audit.checkpoint())


def test_reference_is_explicitly_research_and_rejects_day_change():
    from corridor.bench.bank_cases import BankCase
    from corridor.bench.planning_runner import prepare_transfer
    from corridor.sim.fixtures import make_state

    case = BankCase(make_state(), task_id="UI", subcase_id="reference")
    prepared = prepare_transfer(case, "200/1", step=0)
    token = case.device.approve(
        prepared.action, prepared.cert, prepared.proposal.card, now=case.state.now
    )
    before = case.state
    same = reference_preconditions(prepared.action, prepared.cert, before, before, token=token)
    assert same["status"] == "holds" and same["label"] == "参考判定 · 研究口径"
    changed = before.model_copy(update={"now": add_seconds(before.now, 86400)})
    result = reference_preconditions(prepared.action, prepared.cert, before, changed, token=token)
    assert result["status"] == "changed" and "business_date" in result["changed"]
    assert "token_expired" in result["changed"]


def test_reference_excludes_ledger_balance_value_but_checks_payment_floor():
    from corridor.bench.bank_cases import BankCase
    from corridor.bench.planning_runner import prepare_transfer
    from corridor.schemas.bank import AccountMovement, BusinessEvent
    from corridor.sim.fixtures import make_state

    case = BankCase(make_state(), task_id="UI", subcase_id="balance-reference")
    prepared = prepare_transfer(case, "200/1", step=0)
    before = case.state
    for amount, expected in [("49/1", "holds"), ("9900/1", "changed")]:
        debit = BusinessEvent(
            id="UI_REFERENCE_DEBIT",
            intent_key=H("reference-debit"),
            event="settled",
            kind="subscription_debit",
            principal=case.device.principal,
            ts=before.now,
            debit=AccountMovement(account_ref="A_CURRENT", amount=amount),
        )
        current = before.model_copy(update={"ledger": [*before.ledger, debit]})
        result = reference_preconditions(prepared.action, prepared.cert, before, current)
        assert result["status"] == expected
        assert ("available_after_payment_below_floor" in result["changed"]) == (
            expected == "changed"
        )


def test_chain_html_escapes_source_evidence_and_keeps_all_five_columns():
    from corridor.ui.chain_consistency import consistency_html

    controller = DemoController()
    controller.start_session()
    rows = chain_rows(controller, controller.preview_birthday())
    rows[0]["label"] = '<script>alert("injection")</script>'
    output = consistency_html(rows)
    assert "<script>" not in output and "&lt;script&gt;" in output
    for title in ("原始意图", "当前证据", "有效授权", "影响传播", "双判断", "参考判定 · 研究口径"):
        assert title in output


def test_chain_translation_only_changes_display_and_preserves_css():
    from corridor.i18n import reset_locale, set_locale
    from corridor.ui.chain_consistency import consistency_html

    controller = DemoController()
    controller.start_session()
    draft = controller.preview_birthday()
    before = H(controller.state)
    token = set_locale("en")
    try:
        rows = chain_rows(controller, draft)
        output = consistency_html(rows)
        assert "Original intent" in output and "Reference judgment · research" in output
        assert ".chain-evidence{width:100%" in output
        assert all(row["actual"]["status"] == "not_dispatched" for row in rows)
        assert all(row["reference"]["status"] == "not_evaluated" for row in rows)
        assert H(controller.state) == before
    finally:
        reset_locale(token)


def test_multiple_plans_link_to_their_own_escaped_user_words():
    import re

    from corridor.ui.chain_consistency import consistency_html

    controller = DemoController()
    controller.start_session()
    first = controller.preview_birthday(request_id="first-source")
    first_text = 'First request <script>alert("not code")</script>'
    controller._plans[first["plan_id"]]["source_text"] = first_text
    controller.confirm_plan(first["plan_id"], [node["id"] for node in first["nodes"]])
    second = controller.preview_birthday(request_id="second-source")
    second_text = "Second request & a different signed intent"
    controller._plans[second["plan_id"]]["source_text"] = second_text
    before = H(controller.state), controller.gateway.audit.checkpoint()
    rows = chain_rows(controller, second, source_text="Unrelated session quote")
    output = consistency_html(rows)
    links = re.findall(r'href="#([^"]+)"', output)
    targets = re.findall(r'<section id="([^"]+)"', output)
    assert len(rows) == 8 and len(targets) == 2 and targets[0] != targets[1]
    assert links == [targets[0]] * 4 + [targets[1]] * 4
    assert all(row["source_text"] == first_text for row in rows[:4])
    assert all(row["source_text"] == second_text for row in rows[4:])
    assert "Unrelated session quote" not in output
    assert "<script>" not in output and "&lt;script&gt;" in output
    assert "Second request &amp; a different signed intent" in output
    assert before == (H(controller.state), controller.gateway.audit.checkpoint())


def test_missing_plan_quote_is_an_explicit_preset_not_another_plans_words():
    from corridor.ui.chain_consistency import consistency_html

    controller = DemoController()
    controller.start_session()
    first = controller.preview_birthday()
    controller._plans[first["plan_id"]]["source_text"] = "Known first source"
    controller.confirm_plan(first["plan_id"], [node["id"] for node in first["nodes"]])
    second = controller.preview_birthday()
    rows = chain_rows(controller, second, source_text="Unrelated legacy fallback")
    assert all(row["source_text"] is None for row in rows[4:])
    output = consistency_html(rows)
    assert "预设来源（不是用户原话）" in output
    assert "Unrelated legacy fallback" not in output


def test_source_argument_is_only_a_fallback_for_one_unsigned_draft():
    controller = DemoController()
    controller.start_session()
    draft = controller.preview_birthday()
    rows = chain_rows(controller, draft, source_text="Single draft source")
    assert all(row["source_text"] == "Single draft source" for row in rows)
    controller._plans[draft["plan_id"]]["source_text"] = "Retained source wins"
    rows = chain_rows(controller, draft, source_text="Single draft source")
    assert all(row["source_text"] == "Retained source wins" for row in rows)
    controller.confirm_plan(draft["plan_id"], [node["id"] for node in draft["nodes"]])
    controller._plans[draft["plan_id"]].pop("source_text")
    rows = chain_rows(controller, draft, source_text="Unretained historical quote")
    assert all(row["source_text"] is None for row in rows)


def test_budget_render_exposes_existing_clock_control_without_policy_changes(tmp_path):
    from unittest.mock import patch

    with patch("tempfile.mkdtemp", return_value=str(tmp_path)):
        from streamlit.testing.v1 import AppTest

    source = tmp_path / "chain_app.py"
    source.write_text(
        """
import streamlit as st
from corridor.ui.controller import DemoController
from corridor.ui.chain_consistency import render_approval_budget, render_chain_consistency
if "controller" not in st.session_state:
    st.session_state.controller = DemoController()
    st.session_state.controller.start_session()
    st.session_state.draft = st.session_state.controller.preview_birthday()
render_approval_budget(st.session_state.controller)
render_chain_consistency(st.session_state.controller, st.session_state.draft,
                         source_text="我爱人生日快到了，帮我安排一下")
""",
        encoding="utf-8",
    )
    page = AppTest.from_file(str(source), default_timeout=30).run()
    assert not page.exception
    controller = page.session_state["controller"]
    previous = controller.state
    page.button(key="birthday_advance_600").click().run()
    assert not page.exception
    assert controller.state.now == add_seconds(previous.now, 600)
    assert controller.state.policy == previous.policy
    assert any("参考判定 · 研究口径" in item.value for item in page.markdown)
    assert any(
        "120" in item.value and "6" in item.value and "赎回" in item.value for item in page.caption
    )
