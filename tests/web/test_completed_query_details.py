"""Completed read-only tasks tuck generic controls into customer task details."""

import pytest

from tests.web.test_viewmodels import BALANCE, YELLOW, Web

pytestmark = pytest.mark.usefixtures("st_sentinel")


@pytest.mark.parametrize("text", [BALANCE, "看看9月账单", "有没有异常扣款", "查询交易记录"])
@pytest.mark.parametrize("view", ["customer", "judge"])
def test_completed_queries_only_fold_for_customers(text, view):
    web = Web(view=view).login()
    web.send(text)
    panel = web.state()["chat"]["task_panel"]
    assert panel.get("controls_in_details", False) is (view == "customer")
    assert panel["controls"]["stop"] and panel["controls"]["handoff"]


def test_pending_queries_and_writes_keep_their_controls():
    web = Web().login()
    web.send("查看我的订阅")
    assert not web.state()["chat"]["task_panel"].get("controls_in_details", False)
    web.gesture()
    assert web.state()["chat"]["task_panel"]["controls_in_details"]
    web.send(YELLOW)
    assert not web.state()["chat"]["task_panel"].get("controls_in_details", False)
    web.gesture()
    assert not web.state()["chat"]["task_panel"].get("controls_in_details", False)


def test_completed_query_after_a_write_keeps_compensation_in_details():
    web = Web().login()
    web.send(YELLOW)
    web.gesture()
    web.send("看看9月账单")
    panel = web.state()["chat"]["task_panel"]
    assert panel["controls_in_details"]
    assert panel["controls"]["compensate_hint"]


@pytest.mark.parametrize(
    ("tool", "status"), [("transfer", "settled"), ("balance_query", "pending")]
)
def test_mixed_or_incomplete_plans_do_not_fold_controls(monkeypatch, tool, status):
    from corridor.web.vm import chat

    web = Web().login()
    web.send(BALANCE)
    monkeypatch.setattr(
        chat,
        "task_plans",
        lambda *_: [
            {
                "plan": {"nodes": [{"node_id": "current", "tool": tool, "depends_on": []}]},
                "nodes": {"current": {"status": status}},
            }
        ],
    )
    assert not web.state()["chat"]["task_panel"].get("controls_in_details", False)
