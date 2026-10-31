"""A long conversation stays cheap to read (RW-9 item 4).

``/api/state`` grew with the conversation: ``controller.snapshot`` asked the gateway for each
plan, and each answer read and verified the whole audit log twice, so one read cost plans
times records; every message adds a plan and about six records. Twenty short messages took
1.4 s a read and thirty long ones 2.9 s, and the gateway's process-wide lock made other
sessions wait as well. The gateway is frozen, so the read was narrowed above it: one verified
copy of the log and one copy of the state per read.

The tests count the expensive calls instead of timing them: a read makes the same number
of them however long the conversation is. ``test_the_snapshot_holds_the_gateways_plans`` runs
every equivalence flow and compares the narrowed value with the gateway's own at each stage.
"""

from collections import Counter

import pytest

from corridor.gateway.audit import AuditLog
from corridor.stores.bank import BankStore
from corridor.web import compat
from tests.web.conftest import ws_of
from tests.web.drivers import WebDriver, run_step
from tests.web.flows import FLOWS
from tests.web.test_api_chat import Browser
from tests.web.test_tasks import RED

BALANCE = "查询余额"


@pytest.fixture
def calls(monkeypatch):
    """How many whole-log verifications and full state rebuilds have run."""
    seen = Counter()
    verify, snapshot = AuditLog.verify, BankStore.snapshot

    def counted_verify(self, checkpoint=None):
        seen["verify"] += 1
        return verify(self, checkpoint)

    def counted_snapshot(self):
        seen["state"] += 1
        return snapshot(self)

    monkeypatch.setattr(AuditLog, "verify", counted_verify)
    monkeypatch.setattr(BankStore, "snapshot", counted_snapshot)
    return seen


@pytest.fixture
def page(web_app):
    browser = Browser(web_app, view="customer")
    browser.login()
    return browser


def read(page, calls):
    calls.clear()
    page.get("/api/state")
    return dict(calls)


@pytest.mark.parametrize("view", ["customer", "judge"])
def test_a_read_costs_the_same_however_long_the_conversation(web_app, calls, view):
    page = Browser(web_app, view=view)
    page.login()
    page.send(BALANCE)
    first = read(page, calls)
    for _ in range(6):
        page.send(BALANCE)
    assert read(page, calls) == first
    plans = len(ws_of(web_app, page.client).controller.snapshot()["plans"])
    assert plans == 7


def test_a_snapshot_verifies_the_log_a_fixed_number_of_times(page, web_app, calls):
    for _ in range(4):
        page.send(BALANCE)
    controller = ws_of(web_app, page.client).controller
    calls.clear()
    controller.snapshot()
    assert calls == {"verify": 3, "state": 1}


def test_the_compensation_version_is_the_one_the_click_is_checked_against(page, web_app):
    """The panel's version comes from this read's plans, the click's from a new snapshot."""
    page.send(BALANCE)
    panel = page.state["chat"]["task_panel"]["controls"]
    ws = ws_of(web_app, page.client)
    from corridor.web import tasks

    plan_id = tasks.compensation_plan(ws)
    assert plan_id is not None
    assert panel["compensate_version"] == tasks.list_version(ws, "compensation", [plan_id])


@pytest.mark.parametrize("name", sorted(FLOWS))
def test_the_snapshot_holds_the_gateways_plans(name):
    """Every equivalence flow, at every stage: registered order, node statuses, receipts,
    ownership epochs, checkpoints and the blocked flag, as ``gateway.plan_snapshot`` gives."""
    flow = FLOWS[name]
    driver = WebDriver(features=flow.features)
    try:

        def same():
            controller = driver.controller
            gateway = controller.gateway
            held = [gateway.plan_snapshot(ref) for ref in controller._registered_plan_ids()]
            assert controller.snapshot()["plans"] == held

        same()
        for step in flow.steps:
            run_step(driver, step)
            same()
    finally:
        driver.close()


def test_a_flow_with_a_blocked_plan_is_among_them():
    """The comparison above reaches a plan the gateway holds as blocked."""
    driver = WebDriver()
    try:
        driver.send(RED)
        driver.confirm()
        driver.stop_all()
        controller = driver.controller
        plans = controller.snapshot()["plans"]
        assert plans and any(plan["blocked"] for plan in plans)
        gateway = controller.gateway
        assert plans == [gateway.plan_snapshot(ref) for ref in controller._registered_plan_ids()]
    finally:
        driver.close()


def plain_controller():
    controller = compat.DemoController(initial_state=compat.demo_state(showcase=True))
    controller.start_session()
    return controller


def test_a_plan_the_gateway_cannot_show_is_left_out_as_before(page, web_app, monkeypatch):
    page.send(BALANCE)
    page.send(BALANCE)
    controller = ws_of(web_app, page.client).controller
    plans = controller.snapshot()["plans"]
    assert len(plans) == 2
    first = plans[0]["plan"]["plan_id"]
    gateway = controller.gateway
    held = gateway._plans.pop(first)
    try:
        assert [item["plan"]["plan_id"] for item in controller.snapshot()["plans"]] == [
            plans[1]["plan"]["plan_id"]
        ]
    finally:
        gateway._plans[first] = held

    def broken(plan, records):
        raise ValueError("PLAN-SCOPE")

    monkeypatch.setattr("corridor.ui.controller.project_runtime", broken)
    assert controller.snapshot()["plans"] == []


def test_a_broken_audit_log_still_fails_the_read():
    controller = plain_controller()
    controller.gateway.audit._broken = True
    with pytest.raises(ValueError, match="AUD-CHAIN"):
        controller.snapshot()


def test_a_snapshot_hands_out_its_own_copies(page, web_app):
    page.send(BALANCE)
    controller = ws_of(web_app, page.client).controller
    first = controller.snapshot()
    first["audit"].clear()
    first["plans"][0]["audit_checkpoint"]["seq"] = -1
    again = controller.snapshot()
    assert again["audit"] and again["plans"][0]["audit_checkpoint"]["seq"] > 0


def test_a_turn_reads_one_copy_of_the_state(page, calls):
    """The chat's turns share a copy; the words shown are the same as before."""
    for _ in range(4):
        page.send(RED if _ == 0 else BALANCE)
    state = read(page, calls)["state"]
    for _ in range(4):
        page.send(BALANCE)
    assert read(page, calls)["state"] == state
    words = [turn["text"] for turn in page.state["chat"]["turns"] if turn["role"] == "user"]
    assert BALANCE in words
