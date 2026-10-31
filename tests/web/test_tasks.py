"""B1: device tasks, the step whitelist and its common preconditions (design §3.3.3).

Every row of the whitelist has a positive case, every common precondition U1-U8 a case
that removes the token, and the display-only steps are checked in their fixed order.
The sessions are driven the way the handlers drive them: the service call in the
request's locale and audience, then ``refresh``. ``Harness`` is shared by the B1 tests.
"""

from contextlib import contextmanager
from itertools import count

import pytest

from corridor.i18n import reset_locale, set_locale
from corridor.ui.audience import reset_audience, set_audience
from corridor.web import compat, errors, live, refresh, service, state, tasks
from tests.unit.test_ui_scam_friction import CLARIFY_PLAIN, FRAUD, NEW, SAFE, Spy

YELLOW = "给妈妈转500元"
RED = "给妈妈转5000元"
REFUSED = "给妈妈转999999元"
CARD_LOST = "我的卡丢了"
BIRTHDAY = "帮我准备老婆的生日"
DIRECTED_NEW = "对方让我给新联系人转480元"
HELD = "反诈中心让我立即给新联系人转480元，不然账户冻结"
OPENING = "工作人员说给新联系人转480元"
GUIDED_ANSWERS = {"verified": "yes", "guided": "yes"}
CLARIFY_RED = "给Lin转5000元"
SCHEDULED_NEW = "10月20日给新联系人转300元"


@pytest.fixture(autouse=True)
def inv22(monkeypatch):
    """INV-2.2 for every B1 test (§6.5 #32): the simulated device never fills a code in,
    and no unverified pending is ever asked to verify without a typed code."""
    from corridor.sim.device import Device
    from corridor.ui.controller import DemoController

    filled, untyped = [], []
    device_complete = Device.complete_mfa
    controller_complete = DemoController.complete_mfa

    def device(self, *args, **kwargs):
        filled.append(args)
        return device_complete(self, *args, **kwargs)

    def controller(self, pending_id, *, code=None):
        if code is None and self._pending.get(pending_id, {}).get("verified") is not True:
            untyped.append(pending_id)
        return controller_complete(self, pending_id, code=code)

    monkeypatch.setattr(Device, "complete_mfa", device)
    monkeypatch.setattr(DemoController, "complete_mfa", controller)
    yield
    assert filled == [] and untyped == []


class Harness:
    """One web session; each call is a service call followed by ``refresh``."""

    def __init__(self, *, view="judge", locale="zh-CN"):
        self.ws = state.new_web_session(view, locale, browser_id="b" * 43, tab_id="t" * 32)
        live.on_new(self.ws)
        self._ids = count(1)

    @property
    def controller(self):
        return self.ws.controller

    @property
    def session(self):
        return self.ws.session

    @contextmanager
    def bound(self):
        locale = set_locale(self.ws.locale)
        audience = set_audience(self.ws.view)
        try:
            yield
        finally:
            reset_audience(audience)
            reset_locale(locale)

    def call(self, name, *args):
        """As the handler does: refresh after success and after a call that began."""
        self.ws.revision += 1
        with self.bound():
            try:
                result = getattr(service, name)(self.ws, *args)
            except (errors.WebOperationFailed, errors.WebOutcomeUnknown):
                refresh.refresh(self.ws)
                raise
            refresh.refresh(self.ws)
        return result

    def refresh(self):
        with self.bound():
            refresh.refresh(self.ws)

    def login(self):
        return self.call("login")

    def send(self, text, files=0):
        return self.call("send_message", text, files, f"msg-{next(self._ids):04d}")

    def shortcut(self, key):
        return self.call("send_shortcut", key, f"msg-{next(self._ids):04d}")

    def step(self, tkey):
        with self.bound():
            return tasks.task_step(self.ws, self.ws.tasks[tkey]).step

    def info(self, tkey):
        with self.bound():
            return tasks.task_step(self.ws, self.ws.tasks[tkey])

    def listed(self):
        with self.bound():
            return [task.key for task in tasks.collect(self.ws)]

    def identity(self, tkey):
        return self.ws.tasks[tkey].identity

    def token(self, tkey):
        held = self.ws.gestures[tkey]
        return held.token, tasks.card_version(self.ws, held.digest)

    def confirm(self, tkey):
        return self.call("device_confirm", tkey, *self.token(tkey))

    def mfa_request(self, tkey):
        return self.call("device_mfa_request", tkey, *self.token(tkey))

    def mfa_verify(self, tkey, code):
        return self.call("device_mfa_verify", tkey, *self.token(tkey), code)

    def sms_code(self):
        """The code as the simulated phone shows it: only from this pairing's inbox."""
        for message in compat.OUTBOX.inbox(self.ws.pair):
            if message.status == "active":
                return message.code
        return None

    def test_code(self, tkey):
        pending = self.controller._pending[self.identity(tkey)]
        return self.controller.gateway.mfa.test_code(pending["challenge_id"])

    def answer(self, rkey, answers):
        return self.call("gate_answer", rkey, self.ws.gates[rkey][0], answers)

    def retry(self, rkey):
        return self.call("gate_retry", rkey, self.ws.gates[rkey][0])

    def choose(self, rkey, index=0):
        return self.call("select_recipient", rkey, f"c{index}")

    def nodes(self, tkey, keys):
        version = tasks.nodes_version(self.ws, self.ws.tasks[tkey])
        return self.call("select_nodes", tkey, version, keys)

    def red_path(self, tkey):
        """confirm -> request a code -> type the phone's code -> confirm again."""
        assert self.step(tkey) == "confirm"
        self.confirm(tkey)
        assert self.step(tkey) == "mfa_request"
        self.mfa_request(tkey)
        assert self.step(tkey) == "mfa_code"
        code = self.sms_code()
        assert code == self.test_code(tkey)
        assert self.mfa_verify(tkey, code) == {"status": "mfa_verified"}
        assert self.step(tkey) == "final_confirm"
        return self.confirm(tkey)


def logged_in():
    harness = Harness()
    harness.login()
    return harness


def codes_seen(controller, monkeypatch):
    """Every ``code`` the controller's MFA entry receives, with the pending's verified flag."""
    seen = []
    original = controller.complete_mfa

    def wrapper(pending_id, *, code=None):
        seen.append((code, controller._pending.get(pending_id, {}).get("verified") is True))
        return original(pending_id, code=code)

    monkeypatch.setattr(controller, "complete_mfa", wrapper)
    return seen


def no_autocomplete(spy, seen):
    """INV-2.2: the device never fills a code in, and no unverified pending gets None."""
    assert spy.counts["complete_mfa"] == 0
    assert all(code is not None or verified for code, verified in seen), seen


# --- whitelist rows ---------------------------------------------------------------------


def test_a_yellow_transfer_draft_is_the_confirm_row_and_then_done(monkeypatch):
    harness = logged_in()
    rkey = harness.send(YELLOW)
    tkey = "T" + rkey
    info = harness.info(tkey)
    assert (info.step, info.op) == ("confirm", "confirm")
    assert harness.ws.tasks[tkey].kind == "envelope"
    assert tkey in harness.ws.gestures
    spy = Spy(harness.controller, monkeypatch)
    assert harness.confirm(tkey) == {"status": "settled"}
    assert spy.counts["approve"] == 1 and spy.counts["dispatch"] == 1
    assert harness.step(tkey) == "done"
    assert tkey not in harness.ws.gestures
    # The current reply keeps its finished task on the device pane.
    assert harness.listed() == [tkey]


def test_a_red_transfer_walks_the_fixed_order(monkeypatch):
    harness = logged_in()
    tkey = "T" + harness.send(RED)
    spy = Spy(harness.controller, monkeypatch)
    seen = codes_seen(harness.controller, monkeypatch)
    ops = []
    for expected, op in (
        ("confirm", "confirm"),
        ("mfa_request", "mfa_request"),
        ("mfa_code", "mfa_verify"),
    ):
        info = harness.info(tkey)
        assert (info.step, info.op) == (expected, op)
        ops.append(harness.ws.gestures[tkey].op)
        if op == "confirm":
            harness.confirm(tkey)
        elif op == "mfa_request":
            harness.mfa_request(tkey)
        else:
            harness.mfa_verify(tkey, harness.sms_code())
    assert ops == ["confirm", "mfa_request", "mfa_verify"]
    assert (harness.info(tkey).step, harness.info(tkey).op) == ("final_confirm", "confirm")
    assert harness.confirm(tkey) == {"status": "settled"}
    assert spy.counts["dispatch"] == 1 and spy.counts["verify"] == 1
    no_autocomplete(spy, seen)


def test_a_card_operation_preview_is_confirm_intent_then_the_red_rows(monkeypatch):
    harness = logged_in()
    tkey = "T" + harness.send(CARD_LOST)
    assert harness.info(tkey).step == "confirm_intent"
    assert compat.pending_entry(harness.controller, harness.identity(tkey))["mode"] == "draft"
    harness.confirm(tkey)
    assert compat.pending_entry(harness.controller, harness.identity(tkey))["mode"] == "prepared"
    assert harness.step(tkey) == "mfa_request"
    harness.mfa_request(tkey)
    harness.mfa_verify(tkey, harness.sms_code())
    assert harness.step(tkey) == "final_confirm"
    assert harness.confirm(tkey) == {"status": "settled"}


def test_a_chosen_recipient_is_the_local_confirm_row():
    harness = logged_in()
    rkey = harness.send(CLARIFY_PLAIN)
    tkey = "T" + rkey
    assert tkey not in harness.ws.tasks
    harness.choose(rkey, 0)
    task = harness.ws.tasks[tkey]
    assert task.kind == "local" and task.identity == harness.ws.local[task.rid]["draft_id"]
    assert harness.step(tkey) == "confirm"
    assert harness.confirm(tkey) == {"status": "settled"}
    assert harness.ws.local[task.rid]["status"] == "settled"
    assert harness.step(tkey) == "done"


def test_a_prepared_yellow_pending_is_the_plain_confirm_row(monkeypatch):
    harness = logged_in()
    tkey = "T" + harness.send(YELLOW)
    approve = harness.controller.workflow._approve
    drift = {"left": 1}

    def changed(gateway, prepared, expected, display_id):
        if drift["left"]:
            drift["left"] -= 1
            prepared.proposal.card["amount"] = "501/1"
        return approve(gateway, prepared, expected, display_id)

    monkeypatch.setattr(harness.controller.workflow, "_approve", changed)
    assert harness.confirm(tkey)["status"] == "redisplay"
    pending = compat.pending_entry(harness.controller, harness.identity(tkey))
    assert pending["mode"] == "prepared" and pending["tier"] == "yellow"
    assert harness.info(tkey).step == "confirm"
    assert harness.confirm(tkey)["status"] in {"settled", "redisplay"}


def test_plan_rows_follow_the_registered_selection():
    harness = logged_in()
    tkey = "T" + harness.send(BIRTHDAY)
    info = harness.info(tkey)
    assert (info.step, info.op, info.detail["total"]) == ("select_nodes", None, 4)
    assert tkey not in harness.ws.gestures
    harness.nodes(tkey, ["n1"])
    assert harness.info(tkey).step == "select_nodes" and harness.info(tkey).detail["error"]
    assert tkey not in harness.ws.gestures
    harness.nodes(tkey, ["n0"])
    assert (harness.info(tkey).step, harness.ws.gestures[tkey].op) == ("confirm_subset", "confirm")
    harness.nodes(tkey, ["n3", "n1", "n2", "n0"])
    assert (
        harness.ws.selection[tkey]
        == compat.plan_entry(harness.controller, harness.identity(tkey))["node_ids"]
    )
    assert harness.info(tkey).step == "confirm_plan"
    assert harness.confirm(tkey) == {"status": "settled"}
    assert harness.step(tkey) == "done"


# --- common preconditions ---------------------------------------------------------------


def test_u1_no_token_without_a_live_login():
    harness = Harness()
    assert harness.listed() == []
    harness.login()
    tkey = "T" + harness.send(YELLOW)
    assert tkey in harness.ws.gestures
    harness.controller.advance_clock(3601)
    harness.refresh()
    assert harness.listed() == [] and harness.ws.gestures == {}
    assert harness.step(tkey) == "unknown"


def test_u2_a_vanished_action_is_withdrawn():
    harness = logged_in()
    tkey = "T" + harness.send(RED)
    harness.controller.stop()
    harness.refresh()
    assert harness.step(tkey) == "withdrawn" and tkey not in harness.ws.gestures
    harness.ws.tasks[tkey].identity = "not-a-pending"
    assert harness.step(tkey) == "withdrawn"


def test_u3_a_recorded_failure_or_a_closed_phase_closes_the_task():
    harness = logged_in()
    tkey = "T" + harness.send(YELLOW)
    task = harness.ws.tasks[tkey]
    harness.ws.failures[("domestic", task.rid)] = {"live": None, "source": None}
    harness.refresh()
    assert harness.step(tkey) == "closed_failed" and tkey not in harness.ws.gestures
    harness.ws.failures.clear()
    task.phase = "closed_unknown"
    harness.refresh()
    assert harness.step(tkey) == "closed_unknown" and tkey not in harness.ws.gestures
    task.phase = "in_call"
    assert harness.info(tkey).op is None


def test_u4_a_locked_account_has_no_yellow_or_red_token(monkeypatch):
    harness = logged_in()
    tkey = "T" + harness.send(YELLOW)
    monkeypatch.setattr(compat, "safety", lambda controller: {"state": "Locked"})
    harness.refresh()
    assert harness.step(tkey) == "account_locked" and harness.ws.gestures == {}


def test_u5_only_the_current_reply_holds_a_token():
    harness = logged_in()
    first = harness.send(YELLOW)
    second = harness.send(RED)
    assert harness.step("T" + first) == "not_current"
    assert "T" + first not in harness.ws.gestures and "T" + second in harness.ws.gestures
    assert harness.listed() == ["T" + second, "T" + first]
    harness.call("make_current", first)
    assert "T" + first in harness.ws.gestures and "T" + second not in harness.ws.gestures


def test_u6_an_unevaluated_or_unpassed_gate_has_no_token():
    harness = logged_in()
    rkey = harness.send(NEW)
    tkey = "T" + rkey
    identity = harness.identity(tkey)
    assert harness.ws.gate_eval[identity]["result"] == "gated"
    assert harness.info(tkey).detail["decision"] == "incomplete"
    harness.answer(rkey, SAFE)
    assert harness.step(tkey) == "confirm"
    # No evaluation record, or one for another card: not evaluated, so no token.
    record = harness.ws.gate_eval.pop(identity)
    assert harness.step(tkey) == "gate_unevaluated"
    harness.ws.gate_eval[identity] = {**record, "card_identity": "0" * 64}
    assert harness.step(tkey) == "gate_unevaluated"
    harness.ws.gate_eval[identity] = {**record, "result": "n/a"}
    assert harness.step(tkey) == "gate_unevaluated"
    harness.refresh()
    assert harness.step(tkey) == "confirm"


def test_u6_a_failed_establish_leaves_the_task_unevaluated(monkeypatch):
    harness = logged_in()
    rkey = harness.send(YELLOW)

    def broken(*args, **kwargs):
        raise TypeError("establish failed")

    harness.ws.gate_eval.clear()
    monkeypatch.setattr(harness.controller, "scam_gate_establish", broken)
    harness.refresh()
    assert harness.step("T" + rkey) == "gate_unevaluated"
    assert harness.ws.gestures == {}


def test_u7_incomplete_card_facts_close_the_chat_envelope(monkeypatch):
    harness = logged_in()
    tkey = "T" + harness.send(YELLOW)
    original = compat.render_message

    def incomplete(response, *, controller=None):
        return {**original(response, controller=controller), "facts_complete": False}

    monkeypatch.setattr(compat, "render_message", incomplete)
    harness.refresh()
    assert harness.step(tkey) == "closed_facts" and harness.ws.gestures == {}


def test_u8_a_refused_proposal_is_final(monkeypatch):
    harness = logged_in()
    tkey = "T" + harness.send(REFUSED)
    assert harness.confirm(tkey) == {"status": "rejected"}
    assert harness.step(tkey) == "refused" and tkey not in harness.ws.gestures
    assert harness.controller.gateway.bank.execute_count == 0
    # A stored proposal the kernel refused is refused before any code is requested.
    red = "T" + harness.send(RED)
    harness.confirm(red)
    assert harness.step(red) == "mfa_request"
    identity = harness.identity(red)
    refuse = harness.controller.kernel_refusal
    monkeypatch.setattr(
        harness.controller,
        "kernel_refusal",
        lambda pid: (
            {"outcome": "Reject", "rule_ids": [], "dispatches": 0}
            if pid == identity
            else refuse(pid)
        ),
    )
    harness.refresh()
    assert harness.step(red) == "refused" and red not in harness.ws.gestures


# --- display-only steps: their order ----------------------------------------------------


def test_display_steps_keep_their_order(monkeypatch):
    harness = logged_in()
    first = harness.send(NEW)
    tkey = "T" + first
    task = harness.ws.tasks[tkey]
    original = compat.render_message
    monkeypatch.setattr(
        compat,
        "render_message",
        lambda response, *, controller=None: {
            **original(response, controller=controller),
            "facts_complete": False,
        },
    )
    # gate_blocked (8) before closed_facts (9)
    assert harness.step(tkey) == "gate_blocked"
    harness.answer(first, SAFE)
    assert harness.step(tkey) == "closed_facts"
    # gate_unevaluated (7) before gate_blocked (8)
    harness.ws.gate_eval.pop(task.identity)
    assert harness.step(tkey) == "gate_unevaluated"
    # not_current (6) before gate_unevaluated (7)
    harness.ws.current["domestic"] = None
    assert harness.step(tkey) == "not_current"
    # account_locked (5) before not_current (6)
    harness.ws.safety = {"state": "Locked"}
    assert harness.step(tkey) == "account_locked"
    # closed_failed (4) before account_locked (5)
    task.phase = "closed_failed"
    assert harness.step(tkey) == "closed_failed"
    # withdrawn (3) before closed_failed (4)
    harness.controller.stop()
    assert harness.step(tkey) == "withdrawn"


def test_done_and_refused_come_before_every_other_step():
    harness = logged_in()
    tkey = "T" + harness.send(YELLOW)
    harness.confirm(tkey)
    task = harness.ws.tasks[tkey]
    task.phase = "closed_unknown"
    harness.ws.safety = {"state": "Locked"}
    assert harness.step(tkey) == "done"
    refused = "T" + harness.send(REFUSED)
    harness.confirm(refused)
    harness.ws.tasks[refused].phase = "closed_failed"
    assert harness.step(refused) == "refused"


def test_an_envelope_ahead_of_its_pending_never_reaches_final_confirm(monkeypatch):
    """§6.5 #36: the step reads the controller's own pending, never the envelope status."""
    harness = logged_in()
    rkey = harness.send(RED)
    tkey = "T" + rkey
    harness.confirm(tkey)
    harness.mfa_request(tkey)
    envelope = harness.session._responses[harness.ws.rkeys[rkey]]
    envelope["status"] = "mfa_verified"
    envelope["buttons"] = [{"id": "confirm", "response_id": envelope["response_id"]}]
    harness.refresh()
    assert harness.step(tkey) == "mfa_code"
    assert harness.ws.gestures[tkey].op == "mfa_verify"


def test_the_challenge_window_boundary_closes_the_code_step():
    harness = logged_in()
    tkey = "T" + harness.send(RED)
    harness.confirm(tkey)
    harness.mfa_request(tkey)
    harness.controller.advance_clock(119)
    harness.refresh()
    assert harness.step(tkey) == "mfa_code"
    harness.controller.advance_clock(1)
    harness.refresh()
    assert harness.step(tkey) == "closed_challenge" and tkey not in harness.ws.gestures
    assert harness.sms_code() is None


def test_a_second_red_transfer_on_the_same_page_needs_its_own_code(monkeypatch):
    harness = logged_in()
    first = "T" + harness.send(RED)
    assert harness.red_path(first) == {"status": "settled"}
    second = "T" + harness.send(RED)
    harness.confirm(second)
    assert harness.step(second) == "mfa_request"
    assert not compat.pending_verified(harness.controller, harness.identity(second))
    spy = Spy(harness.controller, monkeypatch)
    with pytest.raises(errors.WebError) as caught:
        harness.call("device_confirm", second, "x" * 22, "0" * 32)
    assert caught.value.code == "gesture_mismatch" and spy.total() == 0


def test_withdrawn_and_handed_off_tasks_leave_the_device_pane():
    harness = logged_in()
    stopped = "T" + harness.send(RED)
    harness.call("chat_stop")
    assert harness.step(stopped) == "withdrawn" and stopped not in harness.listed()
    handed = "T" + harness.send(YELLOW)
    harness.call("handoff")
    assert harness.step(handed) == "withdrawn" and handed not in harness.listed()
    assert harness.ws.gestures == {}


def test_the_gate_preset_and_fraud_reply_start_blocked():
    harness = logged_in()
    tkey = "T" + harness.send(DIRECTED_NEW)
    info = harness.info(tkey)
    assert (info.step, info.detail["decision"]) == ("gate_blocked", "pause")
    fraud = harness.send(FRAUD)
    assert harness.info("T" + fraud).detail["decision"] == "incomplete"


# --- tokens and versions ----------------------------------------------------------------


def test_the_digest_covers_identity_step_cards_and_selection():
    harness = logged_in()
    tkey = "T" + harness.send(BIRTHDAY)
    task = harness.ws.tasks[tkey]
    base = tasks.digest(harness.ws, task, tasks.StepInfo("confirm_plan", "confirm", task.identity))
    other_step = tasks.digest(
        harness.ws, task, tasks.StepInfo("confirm_subset", "confirm", task.identity)
    )
    assert base != other_step
    nodes_before = tasks.nodes_version(harness.ws, task)
    harness.nodes(tkey, ["n0"])
    with_selection = tasks.digest(
        harness.ws, task, tasks.StepInfo("confirm_plan", "confirm", task.identity)
    )
    assert with_selection != base
    assert tasks.nodes_version(harness.ws, task) == nodes_before
    assert len(tasks.card_version(harness.ws, base)) == 32
    assert tasks.card_version(harness.ws, base) != tasks.card_version(harness.ws, with_selection)


def test_issue_keeps_an_unchanged_token_and_replaces_a_changed_one():
    harness = logged_in()
    rkey = harness.send(RED)
    tkey = "T" + rkey
    held = harness.ws.gestures[tkey]
    harness.call("set_locale", "en")
    harness.call("set_view", "customer")
    assert harness.ws.gestures[tkey] is held
    harness.confirm(tkey)
    assert harness.ws.gestures[tkey].token != held.token
    assert harness.ws.gestures[tkey].op == "mfa_request"


def test_commit_closes_the_task_by_how_the_call_ended():
    harness = logged_in()
    tkey = "T" + harness.send(YELLOW)
    task = harness.ws.tasks[tkey]
    with tasks.commit(harness.ws, task, "domestic"):
        assert task.phase == "in_call" and tkey not in harness.ws.gestures
    assert task.phase == "idle"
    with pytest.raises(errors.WebOperationFailed):
        with tasks.commit(harness.ws, task, "domestic"):
            raise errors.WebOperationFailed()
    assert task.phase == "closed_failed"
    with pytest.raises(errors.WebOutcomeUnknown):
        with tasks.commit(harness.ws, task, "domestic"):
            raise TypeError("secret detail")
    assert task.phase == "closed_unknown"
