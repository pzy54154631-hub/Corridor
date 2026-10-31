"""B1: the safety negatives of design §6.5 at the service layer (#1-9, 12, 16-23, 27-33).

Zero side effect is judged with the S6 trio: ``snapshot`` (bank, audit, verifier, drafts
and counters), ``protected`` (every hashed or signed object of the action) and ``Spy``
(every issue, verification, signature and dispatch entry).
"""

import pytest

from corridor.crypto.jcs import H
from corridor.ui import scam_friction
from corridor.web import compat, errors, tasks
from tests.unit.test_ui_scam_friction import (
    CLARIFY,
    FRAUD,
    GUIDED,
    NEW,
    SAFE,
    Spy,
    assert_all_refused,
    protected,
    snapshot,
)
from tests.web.test_tasks import (
    BIRTHDAY,
    RED,
    REFUSED,
    YELLOW,
    Harness,
    codes_seen,
    inv22,  # noqa: F401  (INV-2.2 probe)
    logged_in,
    no_autocomplete,
)

FORGED = "A" * 22


def refused(code, call, *args):
    with pytest.raises(errors.WebError) as caught:
        call(*args)
    assert caught.value.code == code, caught.value.code
    return caught.value


class Unchanged:
    """Before/after: the controller, the action's protected bytes, the spies, the token."""

    def __init__(self, harness, tkey, monkeypatch):
        self.harness, self.tkey = harness, tkey
        self.spy = Spy(harness.controller, monkeypatch)
        self.snapshot = snapshot(harness.controller, harness.session)
        self.protected = protected(harness.controller, harness.ws.tasks[tkey].identity)
        self.gesture = harness.ws.gestures.get(tkey)

    def check(self):
        harness = self.harness
        assert self.spy.total() == 0, dict(self.spy.counts)
        assert snapshot(harness.controller, harness.session) == self.snapshot
        identity = harness.ws.tasks[self.tkey].identity
        assert protected(harness.controller, identity) == self.protected


def flip(value):
    return ("1" if value[0] != "1" else "2") + value[1:]


def test_n01_forged_tokens_change_nothing(monkeypatch):
    harness = logged_in()
    other = "T" + harness.send(YELLOW)
    other_token, _ = harness.token(other)
    tkey = "T" + harness.send(RED)
    token, version = harness.token(tkey)
    guard = Unchanged(harness, tkey, monkeypatch)
    refused("gesture_mismatch", harness.call, "device_confirm", tkey, FORGED, version)
    refused("gesture_mismatch", harness.call, "device_confirm", tkey, other_token, version)
    refused("gesture_mismatch", harness.call, "device_mfa_request", tkey, token, version)
    refused("gesture_mismatch", harness.call, "device_mfa_verify", tkey, token, version, "123456")
    refused("gesture_unknown", harness.call, "device_confirm", other, other_token, version)
    guard.check()
    assert harness.ws.gestures[tkey] is guard.gesture
    assert harness.confirm(tkey) == {"status": "waiting_mfa"}


def test_n02_a_replayed_confirmation_reaches_the_controller_once(monkeypatch):
    harness = logged_in()
    tkey = "T" + harness.send(YELLOW)
    token, version = harness.token(tkey)
    spy = Spy(harness.controller, monkeypatch)
    assert harness.call("device_confirm", tkey, token, version) == {"status": "settled"}
    refused("gesture_closed", harness.call, "device_confirm", tkey, token, version)
    assert spy.counts["confirm_transfer"] == 1 and spy.counts["dispatch"] == 1
    red = "T" + harness.send(RED)
    token, version = harness.token(red)
    harness.call("device_confirm", red, token, version)
    refused("gesture_mismatch", harness.call, "device_confirm", red, token, version)
    assert spy.counts["confirm_transfer"] == 2


def test_n03_another_sessions_keys_act_only_on_this_session(monkeypatch):
    first, second = logged_in(), logged_in()
    rkey = first.send(NEW)
    second.send(NEW)
    first.answer(rkey, SAFE)
    tkey = "T" + rkey
    token, version = first.token(tkey)
    gate = first.ws.gates[rkey][0]
    guard_first = Unchanged(first, tkey, monkeypatch)
    guard_second = Unchanged(second, tkey, monkeypatch)
    refused("gesture_unknown", second.call, "device_confirm", tkey, token, version)
    refused("gate_stale", second.call, "gate_answer", rkey, gate, SAFE)
    refused("gate_stale", second.call, "gate_retry", rkey, gate)
    second.answer(rkey, SAFE)
    second_token = second.ws.gestures[tkey].token
    refused("gesture_mismatch", second.call, "device_confirm", tkey, token, version)
    refused("card_changed", second.call, "device_confirm", tkey, second_token, version)
    refused("not_found", second.call, "make_current", "R7")
    guard_first.check()
    assert first.ws.gestures[tkey] is guard_first.gesture
    assert second.ws.gestures[tkey].token == second_token
    assert guard_second.spy.total() == 0


def test_n04_a_tampered_or_old_card_version_is_refused(monkeypatch):
    harness = logged_in()
    tkey = "T" + harness.send(RED)
    token, version = harness.token(tkey)
    guard = Unchanged(harness, tkey, monkeypatch)
    refused("card_changed", harness.call, "device_confirm", tkey, token, flip(version))
    refused("card_changed", harness.call, "device_confirm", tkey, token, version.upper())
    guard.check()
    plan = "T" + harness.send(BIRTHDAY)
    harness.nodes(plan, ["n0"])
    subset_token, subset_version = harness.token(plan)
    harness.nodes(plan, ["n0", "n1", "n2", "n3"])
    token, version = harness.token(plan)
    spy = Spy(harness.controller, monkeypatch)
    refused("card_changed", harness.call, "device_confirm", plan, token, subset_version)
    refused("gesture_mismatch", harness.call, "device_confirm", plan, subset_token, version)
    assert spy.total() == 0
    assert harness.call("device_confirm", plan, token, version) == {"status": "settled"}


@pytest.mark.parametrize("failure", [RuntimeError, TypeError])
def test_n05_a_closed_card_takes_no_gesture(failure, monkeypatch):
    harness = logged_in()
    tkey = "T" + harness.send(YELLOW)
    token, version = harness.token(tkey)

    def broken(*args, **kwargs):
        raise failure("boom")

    monkeypatch.setattr(harness.controller.workflow, "confirm_transfer", broken)
    expected = "operation_failed" if failure is RuntimeError else "outcome_unknown"
    refused(expected, harness.call, "device_confirm", tkey, token, version)
    monkeypatch.undo()
    guard = Unchanged(harness, tkey, monkeypatch)
    refused("gesture_closed", harness.call, "device_confirm", tkey, token, version)
    refused("gesture_closed", harness.call, "device_confirm", tkey, FORGED, version)
    refused("gesture_closed", harness.call, "device_mfa_request", tkey, FORGED, version)
    guard.check()
    assert harness.ws.gestures == {}


@pytest.mark.parametrize("code", [None, "", "12345", "1234567", "12a456", "１２３４５６", 123456])
def test_n06_a_code_that_is_not_six_digits_reaches_no_verifier(code, monkeypatch):
    harness = logged_in()
    tkey = "T" + harness.send(RED)
    harness.confirm(tkey)
    harness.mfa_request(tkey)
    token, version = harness.token(tkey)
    seen = codes_seen(harness.controller, monkeypatch)
    guard = Unchanged(harness, tkey, monkeypatch)
    refused("invalid_input", harness.call, "device_mfa_verify", tkey, token, version, code)
    assert seen == [] and guard.spy.counts["complete_mfa"] == 0
    guard.check()
    assert harness.ws.gestures[tkey] is guard.gesture


def _paused_pending(harness):
    rkey = harness.send(FRAUD)
    harness.answer(rkey, SAFE)
    tkey = "T" + rkey
    harness.confirm(tkey)
    harness.mfa_request(tkey)
    return rkey, tkey


@pytest.mark.parametrize("reason", ["scam_incomplete", "scam_pause", "scam_stale"])
def test_n07_no_gesture_passes_an_unpassed_gate(reason, monkeypatch):
    harness = logged_in()
    rkey, tkey = _paused_pending(harness)
    identity = harness.identity(tkey)
    if reason == "scam_pause":
        harness.answer(rkey, GUIDED)
    elif reason == "scam_incomplete":
        harness.answer(rkey, GUIDED)
        harness.retry(rkey)
    else:
        harness.send(YELLOW)
        authoritative = harness.controller._authoritative_cards

        def drifted(action):
            cards = authoritative(action)
            return [{**card, "amount": "1/1"} for card in cards] if action == identity else cards

        monkeypatch.setattr(harness.controller, "_authoritative_cards", drifted)
        harness.call("make_current", "R2")
    assert tkey not in harness.ws.gestures
    guard = Unchanged(harness, tkey, monkeypatch)
    for name, extra in (
        ("device_confirm", ()),
        ("device_mfa_request", ()),
        ("device_mfa_verify", ("123456",)),
    ):
        with pytest.raises(errors.WebError) as caught:
            harness.call(name, tkey, FORGED, "0" * 32, *extra)
        assert caught.value.status == 409
    guard.check()
    assert_all_refused(
        harness.controller,
        harness.session,
        harness.ws.tasks[tkey].rid,
        identity,
        reason=reason,
        spy=guard.spy,
    )


def test_n08_an_old_gate_version_is_stale_and_records_nothing():
    harness = logged_in()
    rkey = harness.send(FRAUD)
    harness.answer(rkey, GUIDED)
    paused = harness.ws.gates[rkey][0]
    harness.retry(rkey)
    rejections = len(harness.controller.scam_gate_rejections())
    refused("gate_stale", harness.call, "gate_answer", rkey, paused, SAFE)
    refused("gate_stale", harness.call, "gate_retry", rkey, paused)
    assert len(harness.controller.scam_gate_rejections()) == rejections
    status = harness.controller.scam_gate_status(harness.identity("T" + rkey))
    assert status["decision"] == "incomplete" and status["epoch"] == 1


def test_n12_no_service_result_and_no_session_field_carries_the_code(monkeypatch):
    harness = logged_in()
    tkey = "T" + harness.send(RED)
    results = [harness.confirm(tkey), harness.mfa_request(tkey)]
    code = harness.sms_code()
    results.append(harness.mfa_verify(tkey, code))
    results.append(harness.confirm(tkey))
    assert code not in repr(results)
    assert code not in repr({key: value for key, value in vars(harness.ws).items()})


def test_n16_reading_tasks_and_versions_changes_nothing(monkeypatch):
    harness = logged_in()
    rkey = harness.send(FRAUD)
    harness.answer(rkey, SAFE)
    tkey = "T" + rkey
    harness.confirm(tkey)
    harness.mfa_request(tkey)
    guard = Unchanged(harness, tkey, monkeypatch)
    gates, evals = dict(harness.ws.gates), dict(harness.ws.gate_eval)
    gestures = dict(harness.ws.gestures)
    with harness.bound():
        for _ in range(3):
            for task in tasks.collect(harness.ws):
                info = tasks.task_step(harness.ws, task)
                tasks.card_version(harness.ws, tasks.digest(harness.ws, task, info))
                compat.kernel_view(harness.controller, task.identity)
                compat.authoritative_cards(harness.controller, task.identity)
    guard.check()
    assert harness.ws.gates == gates and harness.ws.gate_eval == evals
    assert harness.ws.gestures == gestures


def test_n17_switching_language_or_view_changes_no_state(monkeypatch):
    harness = logged_in()
    rkey = harness.send(FRAUD)
    harness.answer(rkey, SAFE)
    tkey = "T" + rkey
    identity = harness.identity(tkey)
    cards = H(compat.authoritative_cards(harness.controller, identity))
    record = harness.controller.scam_gate_status(identity)
    token, version = harness.token(tkey)
    guard = Unchanged(harness, tkey, monkeypatch)
    for name, value in (("set_locale", "en"), ("set_view", "customer"), ("set_locale", "zh-CN")):
        harness.call(name, value)
    guard.check()
    assert H(compat.authoritative_cards(harness.controller, identity)) == cards
    after = harness.controller.scam_gate_status(identity)
    assert (after["epoch"], after["answers"]) == (record["epoch"], record["answers"])
    assert harness.call("device_confirm", tkey, token, version) == {"status": "waiting_mfa"}


def test_n18_a_client_message_id_is_bound_to_its_request(monkeypatch):
    harness = logged_in()
    calls = []
    receive = harness.session.receive

    def counted(text, *, request_id=None, external_files=0):
        calls.append(text)
        return receive(text, request_id=request_id, external_files=external_files)

    monkeypatch.setattr(harness.session, "receive", counted)
    first = harness.call("send_message", YELLOW, 0, "client-0001")
    assert harness.call("send_message", YELLOW, 0, "client-0001") == first
    assert calls == [YELLOW] and len(harness.ws.transcript) == 2
    refused("msg_id_conflict", harness.call, "send_message", YELLOW, 1, "client-0001")
    refused("msg_id_conflict", harness.call, "send_message", RED, 0, "client-0001")
    refused("msg_id_conflict", harness.call, "send_shortcut", "transfer", "client-0001")
    shortcut = harness.call("send_shortcut", "transfer", "client-0002")
    assert harness.call("send_shortcut", "transfer", "client-0002") == shortcut
    assert len(calls) == 2
    refused("not_found", harness.call, "send_shortcut", "lottery", "client-0003")


def test_n19_a_reply_whose_gate_was_never_established_gets_no_token(monkeypatch):
    harness = logged_in()
    first = harness.send(CLARIFY)
    harness.send(YELLOW)
    spy = Spy(harness.controller, monkeypatch)
    refused("not_current", harness.call, "select_recipient", first, "c0")
    harness.call("make_current", first)
    harness.ws.gate_eval.clear()
    monkeypatch.setattr(
        harness.controller,
        "scam_gate_establish",
        lambda *args, **kwargs: (_ for _ in ()).throw(TypeError("down")),
    )
    harness.choose(first, 0)
    tkey = "T" + first
    assert harness.step(tkey) == "gate_unevaluated" and tkey not in harness.ws.gestures
    refused("gesture_unknown", harness.call, "device_confirm", tkey, FORGED, "0" * 32)
    assert spy.total() == 0
    monkeypatch.undo()
    harness.refresh()
    assert harness.step(tkey) == "gate_blocked"


def test_n20_an_older_reply_answers_nothing_and_holds_no_token():
    harness = logged_in()
    old = harness.send(FRAUD)
    version = harness.ws.gates[old][0]
    harness.send(BIRTHDAY)
    refused("not_current", harness.call, "gate_answer", old, version, SAFE)
    refused("not_current", harness.call, "gate_retry", old, version)
    refused("not_current", harness.call, "choose_birthday", old, "c0")
    refused("not_current", harness.call, "complete_aa", old, ["c0"])
    assert "T" + old not in harness.ws.gestures
    assert harness.step("T" + old) == "not_current"


def test_n21_the_words_are_fixed_when_sent(monkeypatch):
    harness = logged_in()
    shortcut = harness.shortcut("transfer")
    rkey = harness.send(FRAUD)
    harness.answer(rkey, SAFE)
    hints = [(turn.id, turn.hint) for turn in harness.ws.transcript]
    identity = harness.identity("T" + rkey)
    record = harness.controller.scam_gate_status(identity)
    token = harness.ws.gestures["T" + rkey].token
    harness.call("set_locale", "en")
    harness.call("make_current", rkey)
    assert [(turn.id, turn.hint) for turn in harness.ws.transcript] == hints
    after = harness.controller.scam_gate_status(identity)
    assert (after["epoch"], after["answers"], after["context"]) == (
        record["epoch"],
        record["answers"],
        record["context"],
    )
    assert harness.ws.gestures["T" + rkey].token == token
    assert harness.ws.turn_of(shortcut).hint != harness.ws.turn_of(rkey).hint


def test_n22_a_locked_account_offers_no_gesture_until_a_new_session(monkeypatch):
    harness = logged_in()
    tkey = "T" + harness.send(RED)
    harness.confirm(tkey)
    harness.mfa_request(tkey)
    code = harness.sms_code()
    for value in [item for item in ("000000", "111111", "222222", "333333") if item != code][:3]:
        harness.mfa_verify(tkey, value)
    assert harness.ws.safety["state"] == "Locked"
    yellow = "T" + harness.send(YELLOW)
    guard = Unchanged(harness, yellow, monkeypatch)
    refused("gesture_unknown", harness.call, "device_confirm", yellow, FORGED, "0" * 32)
    guard.check()
    fresh = logged_in()
    again = "T" + fresh.send(YELLOW)
    assert fresh.confirm(again) == {"status": "settled"}


def test_n23_the_recipient_cannot_change_under_a_card(monkeypatch):
    harness = logged_in()
    rkey = harness.send(CLARIFY)
    harness.choose(rkey, 0)
    harness.answer(rkey, SAFE)
    harness.confirm("T" + rkey)
    refused("not_applicable", harness.call, "select_recipient", rkey, "c1")
    rid = harness.ws.rkeys[rkey]
    assert harness.ws.local[rid]["status"] == "settled"


def test_n27_a_precheck_failure_consumes_no_token(monkeypatch):
    harness = logged_in()
    tkey = "T" + harness.send(RED)
    token, version = harness.token(tkey)
    refused("card_changed", harness.call, "device_confirm", tkey, token, flip(version))
    refused("gesture_mismatch", harness.call, "device_mfa_request", tkey, token, version)
    assert harness.ws.gestures[tkey].token == token
    harness.confirm(tkey)

    def broken(*args, **kwargs):
        raise AssertionError("broken invariant")

    monkeypatch.setattr(harness.controller, "request_mfa", broken)
    error = refused("outcome_unknown", harness.mfa_request, tkey)
    assert error.status == 500 and error.with_state
    monkeypatch.undo()
    guard = Unchanged(harness, tkey, monkeypatch)
    refused("gesture_closed", harness.call, "device_mfa_request", tkey, FORGED, version)
    guard.check()


def test_n29_a_refused_proposal_takes_no_code_request(monkeypatch):
    harness = logged_in()
    tkey = "T" + harness.send(REFUSED)
    assert harness.confirm(tkey) == {"status": "rejected"}
    guard = Unchanged(harness, tkey, monkeypatch)
    refused("gesture_closed", harness.call, "device_mfa_request", tkey, FORGED, "0" * 32)
    refused("gesture_closed", harness.call, "device_confirm", tkey, FORGED, "0" * 32)
    guard.check()
    assert harness.controller.gateway.bank.execute_count == 0


def test_n31_a_tampered_plan_selection_never_widens_what_is_signed(monkeypatch):
    harness = logged_in()
    plan = "T" + harness.send(BIRTHDAY)
    other = Harness()
    other.login()
    other_plan = "T" + other.send(BIRTHDAY)
    other_version = tasks.nodes_version(other.ws, other.ws.tasks[other_plan])
    version = tasks.nodes_version(harness.ws, harness.ws.tasks[plan])
    refused("card_changed", harness.call, "select_nodes", plan, other_version, ["n0"])
    refused("bad_json", harness.call, "select_nodes", plan, version, ["n0", "n0"])
    refused("not_found", harness.call, "select_nodes", plan, version, ["n7"])
    refused("not_found", harness.call, "select_nodes", plan, version, ["x0"])
    harness.call("select_nodes", plan, version, ["n2", "n0", "n1"])
    shown = compat.plan_entry(harness.controller, harness.identity(plan))["node_ids"]
    assert harness.ws.selection[plan] == shown[:3]
    token, three = harness.token(plan)
    harness.call("select_nodes", plan, version, ["n0", "n1", "n2", "n3"])
    spy = Spy(harness.controller, monkeypatch)
    refused("gesture_mismatch", harness.call, "device_confirm", plan, token, three)
    refused("card_changed", harness.call, "device_confirm", plan, harness.token(plan)[0], three)
    assert spy.total() == 0
    signed = []
    original = harness.controller.workflow.confirm_plan

    def record(gateway, display_id, *, selected_nodes, request_id):
        signed.append(list(selected_nodes))
        return original(gateway, display_id, selected_nodes=selected_nodes, request_id=request_id)

    monkeypatch.setattr(harness.controller.workflow, "confirm_plan", record)
    assert harness.confirm(plan) == {"status": "settled"}
    assert signed == [shown]


def test_n32_no_core_flow_lets_the_device_fill_in_a_code(monkeypatch):
    harness = logged_in()
    spy = Spy(harness.controller, monkeypatch)
    seen = codes_seen(harness.controller, monkeypatch)
    harness.confirm("T" + harness.send(YELLOW))
    harness.red_path("T" + harness.send(RED))
    rkey = harness.send(FRAUD)
    harness.answer(rkey, SAFE)
    harness.red_path("T" + rkey)
    card = "T" + harness.send("我的卡丢了")
    harness.confirm(card)
    harness.mfa_request(card)
    harness.mfa_verify(card, harness.sms_code())
    harness.confirm(card)
    clarify = harness.send(CLARIFY)
    harness.choose(clarify, 0)
    harness.answer(clarify, SAFE)
    if harness.confirm("T" + clarify)["status"] == "waiting_mfa":
        # The day's earlier transfers raise this one to red: the same three steps follow.
        harness.mfa_request("T" + clarify)
        harness.mfa_verify("T" + clarify, harness.sms_code())
        harness.confirm("T" + clarify)
    assert spy.counts["dispatch"] == 5
    no_autocomplete(spy, seen)
    assert any(code is not None for code, _ in seen)


def _three_runs(monkeypatch, gate):
    harness = logged_in()
    if gate == "off":
        monkeypatch.setattr(scam_friction, "level", lambda *args, **kwargs: 0)
    rkey = harness.send(FRAUD)
    tkey = "T" + rkey
    first = harness.session.response(harness.ws.rkeys[rkey])
    at_card = snapshot(harness.controller, harness.session)
    if gate == "off":
        assert harness.controller.scam_gate_status(harness.identity(tkey)) is None
        assert harness.step(tkey) == "confirm"
    else:
        assert harness.step(tkey) == "gate_blocked" and tkey not in harness.ws.gestures
        harness.answer(rkey, SAFE if gate == "proceed" else GUIDED)
    if gate == "pause":
        return harness, tkey, at_card, first
    harness.red_path(tkey)
    return harness, tkey, at_card, first


def _outcome(harness, tkey, first):
    controller = harness.controller
    final = harness.session.response(harness.ws.tasks[tkey].rid)
    return (
        final["status"],
        first["tier"],
        H(first["card"]),
        tuple(final["payload"].get("rule_ids", [])),
        tuple(row["event"] for row in controller.gateway.audit.records()),
        controller.gateway.audit.checkpoint()["head"],
        controller.gateway.bank.execute_count,
        controller.gateway.safety_snapshot(controller.device.principal)["state"],
    )


def test_n33_three_runs_gate_off_proceed_and_pause(monkeypatch):
    harness, tkey, _, first = _three_runs(monkeypatch, "proceed")
    proceed = _outcome(harness, tkey, first)
    assert proceed[0] == "settled" and proceed[6] == 1
    with monkeypatch.context() as patched:
        off_harness, off_tkey, _, off_first = _three_runs(patched, "off")
        off = _outcome(off_harness, off_tkey, off_first)
    assert off == proceed
    harness, tkey, at_card, _ = _three_runs(monkeypatch, "pause")
    assert harness.step(tkey) == "gate_blocked" and harness.ws.gestures == {}
    assert snapshot(harness.controller, harness.session) == at_card
    budget = harness.controller.gateway.approval_budget(harness.controller.device.principal)
    assert budget["used"] == 0
    assert harness.controller.gateway.mfa._challenges == {}
    assert harness.controller.gateway.bank.execute_count == 0
    harness.call("device_cancel", tkey)
    assert harness.controller.at_transaction_boundary()
