"""Product card and DAG projections preserve exact evidence without exposing it by default."""

from contextlib import contextmanager
from copy import deepcopy
from fractions import Fraction
from xml.etree import ElementTree

import pytest

from corridor.i18n import reset_locale, set_locale
from corridor.ui import components
from corridor.ui.controller import DemoController
from corridor.ui.dag import dag_svg, draft_snapshot
from corridor.ui.structured_transfer import preview_structured_transfer


class DisplayRecorder:
    def __init__(self, controller=None):
        self.session_state = {"controller": controller} if controller else {}
        self.depth = 0
        self.visible = []
        self.evidence = []

    @contextmanager
    def expander(self, label, *, expanded=False):
        assert not expanded
        self.depth += 1
        try:
            yield
        finally:
            self.depth -= 1

    def __getattr__(self, name):
        def record(value, *args, **kwargs):
            (self.evidence if self.depth else self.visible).append(str(value))

        return record


@pytest.mark.parametrize("locale,amount", [("zh-CN", "¥800.00"), ("en", "CNY 800.00")])
def test_card_visible_projection_has_no_bindings_or_canonical_fractions(
    monkeypatch, locale, amount
):
    controller = DemoController()
    controller.start_session()
    card = preview_structured_transfer(controller, "A_CURRENT", "CONTACT_LIN_B", "800", "dux-card")[
        "card"
    ]
    before = deepcopy(card)
    display = DisplayRecorder(controller)
    monkeypatch.setattr(components, "st", display)
    monkeypatch.setattr(components, "render_explanation", lambda **kwargs: None)
    token = set_locale(locale)
    try:
        components.render_card(card, position=1, total=2)
    finally:
        reset_locale(token)
    visible = " ".join(display.visible)
    evidence = " ".join(display.evidence)
    assert amount in visible
    for private in ("800/1", card["node_id"], card["action_hash"], card["objects"]["amount_ref"]):
        assert private not in visible
        assert private in evidence
    assert "1" in visible and "2" in visible
    assert "≤" in visible
    assert card == before


def test_threshold_reason_uses_the_current_signed_policy_and_recorded_total():
    controller = DemoController()
    controller.start_session()
    controller.gateway.store.update(
        policy=controller.state.policy.model_copy(update={"transfer_yellow_limit": Fraction(500)})
    )
    card = preview_structured_transfer(
        controller, "A_CURRENT", "CONTACT_LIN_B", "800", "dux-threshold"
    )["card"]
    token = set_locale("en")
    try:
        reason = components.tier_reason(card, controller=controller)
    finally:
        reset_locale(token)
    assert "800.00" in reason and "500.00" in reason and ">" in reason
    assert "MFA" in reason


def test_fact_rows_do_not_render_nested_raw_json_or_identifiers(monkeypatch):
    display = DisplayRecorder()
    monkeypatch.setattr(components, "st", display)
    components.fact_rows({
        "amount": "801/4",
        "node_id": "79f1a2cd-743d-5676-b151-af7c66b55b54",
        "depends_on": ["79f1a2cd-743d-5676-b151-af7c66b55b54"],
        "risk": {"level": 2},
    })
    visible = " ".join(display.visible)
    assert "200.25" in visible
    assert "801/4" not in visible and "79f1a2cd" not in visible
    assert "{&quot;" not in visible and "[&quot;" not in visible


def test_dag_links_use_ordinals_and_localized_business_names_without_machine_ids():
    controller = DemoController()
    controller.start_session()
    snapshot = draft_snapshot(controller.preview_birthday())
    before = deepcopy(snapshot)
    token = set_locale("zh-CN")
    try:
        svg = dag_svg(snapshot, key="birthday")
    finally:
        reset_locale(token)
    tree = ElementTree.fromstring(svg)
    ns = {"svg": "http://www.w3.org/2000/svg"}
    links = tree.findall(".//svg:a", ns)
    assert len(links) == 4
    assert {link.attrib["href"] for link in links} == {
        f"#dag-birthday-node-{index}" for index in range(1, 5)
    }
    assert len(tree.findall(".//svg:path[@data-edge]", ns)) == 3
    assert "生日" in svg and "鲜花" in svg and "蛋糕" in svg
    assert not any(node["node_id"] in svg for node in snapshot["plan"]["nodes"])
    assert snapshot == before


@pytest.mark.parametrize(
    "status",
    ["pending", "waiting_approval", "running", "succeeded", "failed", "compensated", "handoff"],
)
def test_dag_retains_the_gateway_node_state(status):
    snapshot = {
        "plan": {"nodes": [{"node_id": "node", "tool": "transfer", "depends_on": []}]},
        "nodes": {"node": {"status": status}},
    }
    svg = dag_svg(snapshot)
    assert f'data-status="{status}"' in svg


@pytest.mark.parametrize(
    "status,expected,color",
    [
        ("waiting_mfa", "waiting_mfa", "#fff0c7"),
        ("mfa_verified", "waiting_approval", "#fff0c7"),
        ("unknown", "unknown", "#fde7cc"),
        ("waiting_settlement", "waiting_settlement", "#d9ebff"),
        ("failed", "failed", "#fbe1e1"),
        ("rejected", "blocked", "#fbe1e1"),
        ("refused", "blocked", "#fbe1e1"),
        ("interrupted", "interrupted", "#e8e8ed"),
        ("withdrawn", "cancelled", "#e8e8ed"),
        ("handed_off", "handoff", "#fde7cc"),
        ("handoff", "handoff", "#fde7cc"),
        ("awaiting_device_selection", "waiting_input", "#fff0c7"),
        ("settled", "settled", "#dcefe8"),
    ],
)
def test_single_task_dag_preserves_response_outcome_without_buttons(status, expected, color):
    from corridor.ui.domestic_views import task_node_status

    response = {"status": status, "buttons": []}
    before = deepcopy(response)
    actual = task_node_status(response)
    snapshot = {
        "plan": {"nodes": [{"node_id": "current", "tool": "transfer", "depends_on": []}]},
        "nodes": {"current": {"status": actual}},
    }
    svg = dag_svg(snapshot)
    assert actual == expected
    assert f'data-status="{expected}"' in svg
    assert color in svg
    if status != "settled":
        assert 'data-status="settled"' not in svg
        assert "#dcefe8" not in svg
    assert response == before


@pytest.mark.parametrize("buttons,expected", [([], "pending"), (["confirm"], "waiting_approval")])
def test_unrecognized_task_status_does_not_imply_completion(buttons, expected):
    from corridor.ui.domestic_views import task_node_status

    assert task_node_status({"status": "new_status", "buttons": buttons}) == expected


def _record_task_panel(monkeypatch):
    from corridor.ui import domestic_views

    class TaskPanelRecorder(DisplayRecorder):
        @contextmanager
        def container(self, **kwargs):
            yield

    display = TaskPanelRecorder()
    graphs = []
    monkeypatch.setattr(domestic_views, "st", display)
    monkeypatch.setattr(domestic_views, "task_controls", lambda *args: None)
    monkeypatch.setattr(domestic_views, "render_plans", graphs.extend)
    monkeypatch.setattr(
        domestic_views, "render_dag", lambda snapshot, **kwargs: graphs.append(snapshot)
    )
    return display, graphs


def test_current_mfa_task_never_shows_prior_transfer_plan(monkeypatch):
    from corridor.agent.domestic_session import DomesticSession
    from corridor.ui import domestic_views
    from corridor.ui.domestic_demos import prepare_demo
    from corridor.ui.messages import source_utterance

    controller = prepare_demo("daily_threshold")["controller"]
    session = DomesticSession(controller)
    response = session.receive(source_utterance("Transfer 800 yuan to mom"))
    draft_id = response["payload"]["draft_id"]
    controller.select_contact(draft_id, response["payload"]["contacts"][-1])
    waiting = controller.confirm_transfer(draft_id)
    assert waiting["status"] == "waiting_mfa"
    assert waiting["card"]["total_before"] == "300/1"
    assert len(controller.snapshot()["plans"]) == 2
    dispatches = controller.gateway.bank.execute_count
    display, graphs = _record_task_panel(monkeypatch)
    display.session_state[f"demo_selected:{response['response_id']}"] = waiting
    projected = domestic_views.display_response(session, response["response_id"], "demo")

    domestic_views.task_panel(session, projected, "demo")

    assert len(graphs) == 1
    assert graphs[0]["nodes"] == {"current": {"status": "waiting_mfa"}}
    assert len(graphs[0]["plan"]["nodes"]) == 1
    assert "plan_id" not in graphs[0]["plan"]
    assert session.response(response["response_id"]) == response
    assert controller.gateway.bank.execute_count == dispatches


def test_birthday_result_shows_only_its_actual_plan_snapshot(monkeypatch):
    from corridor.agent.domestic_session import DomesticSession
    from corridor.ui import domestic_views
    from corridor.ui.domestic_demos import prepare_demo

    controller = prepare_demo("daily_threshold")["controller"]
    session = DomesticSession(controller)
    # G06: the partner shortcut no longer plans Chen/mom's birthday; name no relation here.
    birthday = session.receive("帮我安排生日")
    response = session.confirm(
        birthday["response_id"],
        selected_node_ids=[node["id"] for node in birthday["payload"]["nodes"]],
    )
    plans = controller.snapshot()["plans"]
    expected = next(
        plan for plan in plans if plan["plan"]["plan_id"] == response["payload"]["plan_id"]
    )
    assert len(plans) == 2
    assert len(expected["plan"]["nodes"]) == 4
    dispatches = controller.gateway.bank.execute_count
    _, graphs = _record_task_panel(monkeypatch)

    domestic_views.task_panel(session, response, "birthday")

    assert graphs == [expected]
    assert controller.gateway.bank.execute_count == dispatches


def test_dag_technical_details_have_targets_inside_closed_details():
    from corridor.ui.dag import dag_details_html

    snapshot = {
        "plan": {"nodes": [{"node_id": "proof-node", "tool": "transfer", "depends_on": []}]},
        "nodes": {"proof-node": {"status": "unknown", "receipt_ref": "proof-receipt"}},
    }
    markup = dag_details_html(snapshot, key="proof")
    assert '<details class="dag-evidence">' in markup
    assert '<div id="dag-proof-node-1">' in markup
    assert "proof-node" in markup and "proof-receipt" in markup
    assert " open" not in markup


def test_cancel_transfer_uses_existing_session_stop_and_never_executes(monkeypatch):
    from corridor.ui import structured_transfer

    controller = DemoController()
    controller.start_session()
    pending = preview_structured_transfer(
        controller, "A_CURRENT", "CONTACT_LIN_B", "800", "dux-cancel"
    )
    display = DisplayRecorder(controller)
    display.session_state["structured_transfer_pending"] = pending
    monkeypatch.setattr(structured_transfer, "st", display)
    result = structured_transfer.cancel_structured_transfer(controller)
    assert result["status"] == "interrupted"
    assert result["retired_drafts"] == 1
    assert not controller.snapshot()["pending"]
    assert "structured_transfer_pending" not in display.session_state
    assert controller.gateway.bank.execute_count == 0
    assert not controller.state.user_inputs
