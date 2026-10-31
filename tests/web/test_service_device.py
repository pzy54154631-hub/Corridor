"""B1: device gestures end to end at the service layer (design §2.4, §2.5.3, §2.5.4).

Codes are read only from this session's simulated phone inbox and cross-checked with the
verifier's test hook; every red flow ends with the device's own auto-complete untouched.
"""

import pytest

from corridor.web import compat, errors, tasks
from tests.unit.test_ui_scam_friction import Spy, protected, snapshot
from tests.web.test_tasks import (
    BIRTHDAY,
    CLARIFY_RED,
    RED,
    YELLOW,
    codes_seen,
    inv22,  # noqa: F401  (INV-2.2 probe)
    logged_in,
    no_autocomplete,
)


def refused(code, call, *args):
    with pytest.raises(errors.WebError) as caught:
        call(*args)
    assert caught.value.code == code, caught.value.code
    return caught.value


def inbox(harness):
    return [
        (message.status, bool(message.code)) for message in compat.OUTBOX.inbox(harness.ws.pair)
    ]


def test_a_yellow_transfer_needs_one_device_confirmation(monkeypatch):
    harness = logged_in()
    tkey = "T" + harness.send(YELLOW)
    spy = Spy(harness.controller, monkeypatch)
    assert harness.confirm(tkey) == {"status": "settled"}
    assert dict(spy.counts) == {"confirm_transfer": 1, "approve": 1, "dispatch": 1}
    response = harness.session.response(harness.ws.tasks[tkey].rid)
    assert response["status"] == "settled" and response["payload"]["receipt"]
    assert harness.controller.gateway.bank.execute_count == 1
    assert ("domestic", harness.ws.tasks[tkey].rid) not in harness.ws.failures
    assert harness.ws.op_log[-1]["rkey"] == harness.ws.tasks[tkey].rkey


def test_the_red_flow_reads_its_code_only_from_the_phone(monkeypatch):
    harness = logged_in()
    rkey = harness.send(RED)
    tkey = "T" + rkey
    spy = Spy(harness.controller, monkeypatch)
    seen = codes_seen(harness.controller, monkeypatch)
    assert harness.confirm(tkey) == {"status": "waiting_mfa"}
    assert inbox(harness) == []
    result = harness.mfa_request(tkey)
    assert result == {"status": "waiting_mfa"}
    code = harness.sms_code()
    assert code == harness.test_code(tkey) and code not in repr(result)
    assert inbox(harness) == [("active", True)]
    assert harness.ws.tasks[tkey].identity not in repr(result)
    assert harness.mfa_verify(tkey, code) == {"status": "mfa_verified"}
    assert inbox(harness) == [("used", False)]
    envelope = harness.session.response(harness.ws.rkeys[rkey])
    assert envelope["status"] == "mfa_verified"
    assert [button["id"] for button in envelope["buttons"]] == ["confirm"]
    assert harness.confirm(tkey) == {"status": "settled"}
    assert spy.counts["request_mfa"] == 1 and spy.counts["verify"] == 1
    assert spy.counts["dispatch"] == 1
    no_autocomplete(spy, seen)
    # Nothing in the session's own objects holds the code.
    assert code not in repr(vars(harness.ws))


def test_a_second_red_transfer_cannot_reuse_the_first_code(monkeypatch):
    harness = logged_in()
    first = "T" + harness.send(RED)
    harness.confirm(first)
    harness.mfa_request(first)
    old = harness.sms_code()
    harness.mfa_verify(first, old)
    harness.confirm(first)
    second = "T" + harness.send(RED)
    spy = Spy(harness.controller, monkeypatch)
    seen = codes_seen(harness.controller, monkeypatch)
    harness.confirm(second)
    harness.mfa_request(second)
    fresh = harness.sms_code()
    assert fresh != old and fresh == harness.test_code(second)
    if old != fresh:
        assert harness.mfa_verify(second, old) == {"status": "rejected"}
    assert harness.step(second) == "mfa_code"
    assert harness.mfa_verify(second, fresh) == {"status": "mfa_verified"}
    assert harness.confirm(second) == {"status": "settled"}
    no_autocomplete(spy, seen)


def test_three_wrong_codes_lock_the_account_and_withhold_every_token(monkeypatch):
    harness = logged_in()
    tkey = "T" + harness.send(RED)
    harness.confirm(tkey)
    harness.mfa_request(tkey)
    code = harness.sms_code()
    wrong = [value for value in ("000000", "111111", "222222", "333333") if value != code][:3]
    for attempt, value in enumerate(wrong, 1):
        assert harness.mfa_verify(tkey, value) == {"status": "rejected"}
        if attempt < 3:
            assert harness.step(tkey) == "mfa_code"
    assert harness.ws.safety["state"] == "Locked"
    assert harness.step(tkey) == "account_locked" and harness.ws.gestures == {}
    assert inbox(harness) == [("expired", False)]
    spy = Spy(harness.controller, monkeypatch)
    refused("gesture_unknown", harness.call, "device_mfa_verify", tkey, "x" * 22, "0" * 32, code)
    yellow = "T" + harness.send(YELLOW)
    assert harness.step(yellow) == "account_locked" and harness.ws.gestures == {}
    assert spy.total() == 0
    # Green queries remain available.
    query = harness.send("查询余额")
    assert harness.session.response(harness.ws.rkeys[query])["status"] == "settled"


def test_an_expected_failure_closes_the_task_and_cancel_restores_the_boundary(monkeypatch):
    harness = logged_in()
    rkey = harness.send(YELLOW)
    tkey = "T" + rkey

    def broken(*args, **kwargs):
        raise RuntimeError("C:\\secret\\path")

    monkeypatch.setattr(harness.controller.workflow, "confirm_transfer", broken)
    error = refused("operation_failed", harness.confirm, tkey)
    assert error.with_state and "secret" not in str(error)
    task = harness.ws.tasks[tkey]
    assert task.phase == "closed_failed" and ("domestic", task.rid) in harness.ws.failures
    assert harness.step(tkey) == "closed_failed" and tkey not in harness.ws.gestures
    assert not harness.controller.at_transaction_boundary()
    harness.call("device_cancel", tkey)
    assert harness.controller.at_transaction_boundary()
    assert harness.ws.transcript[-2].marker == "cancel"


def test_an_unexpected_failure_closes_the_task_as_unknown(monkeypatch):
    harness = logged_in()
    tkey = "T" + harness.send(RED)
    harness.confirm(tkey)

    def broken(*args, **kwargs):
        raise TypeError("secret")

    monkeypatch.setattr(harness.controller, "request_mfa", broken)
    error = refused("outcome_unknown", harness.mfa_request, tkey)
    assert error.with_state and error.status == 500 and "secret" not in str(error)
    assert harness.ws.tasks[tkey].phase == "closed_unknown"
    assert harness.step(tkey) == "closed_unknown" and tkey not in harness.ws.gestures
    refused("gesture_closed", harness.call, "device_mfa_request", tkey, "x" * 22, "0" * 32)


def test_a_failed_message_is_a_page_notice_never_a_reply_failure(monkeypatch):
    harness = logged_in()

    def broken(text, *, request_id=None, external_files=0):
        raise ValueError("no")

    monkeypatch.setattr(harness.session, "receive", broken)
    refused("operation_failed", harness.send, YELLOW)
    assert harness.ws.transcript == [] and harness.ws.failures == {}
    assert harness.ws.pending_notices and harness.ws.pending_notices[-1]["level"] == "error"


def test_plan_selection_signs_only_the_registered_prefix(monkeypatch):
    harness = logged_in()
    tkey = "T" + harness.send(BIRTHDAY)
    harness.nodes(tkey, ["n0", "n1"])
    assert harness.step(tkey) == "confirm_subset"
    spy = Spy(harness.controller, monkeypatch)
    old = harness.identity(tkey)
    assert harness.confirm(tkey) == {"status": "redisplay"}
    assert spy.total() == 0
    plan = compat.plan_entry(harness.controller, harness.identity(tkey))
    assert harness.identity(tkey) != old and len(plan["node_ids"]) == 2
    assert harness.ws.selection.get(tkey) is None and harness.step(tkey) == "select_nodes"
    signed = []
    original = harness.controller.workflow.confirm_plan

    def record(gateway, display_id, *, selected_nodes, request_id):
        signed.append(list(selected_nodes))
        return original(gateway, display_id, selected_nodes=selected_nodes, request_id=request_id)

    monkeypatch.setattr(harness.controller.workflow, "confirm_plan", record)
    harness.nodes(tkey, ["n1", "n0"])
    assert harness.ws.selection[tkey] == plan["node_ids"]
    assert harness.confirm(tkey) == {"status": "settled"}
    assert signed == [plan["node_ids"]]


def test_a_chosen_recipient_red_flow_keeps_its_local_projection(monkeypatch):
    harness = logged_in()
    rkey = harness.send(CLARIFY_RED)
    harness.choose(rkey, 0)
    tkey = "T" + rkey
    rid = harness.ws.rkeys[rkey]
    spy = Spy(harness.controller, monkeypatch)
    seen = codes_seen(harness.controller, monkeypatch)
    assert harness.red_path(tkey) == {"status": "settled"}
    assert harness.ws.local[rid]["status"] == "settled"
    assert harness.session.response(rid)["status"] == "clarify"
    no_autocomplete(spy, seen)
    assert spy.counts["dispatch"] == 1


def test_cancel_retires_the_local_choice_and_the_live_code():
    harness = logged_in()
    rkey = harness.send(CLARIFY_RED)
    harness.choose(rkey, 1)
    tkey = "T" + rkey
    harness.confirm(tkey)
    harness.mfa_request(tkey)
    assert inbox(harness) == [("active", True)]
    harness.call("device_cancel", tkey)
    assert harness.ws.rkeys[rkey] not in harness.ws.local
    assert inbox(harness) == [("expired", False)]
    assert harness.controller.at_transaction_boundary()
    stop = harness.ws.transcript[-1]
    assert stop.role == "assistant" and harness.ws.current["domestic"] == stop.rid
    assert harness.session.response(stop.rid)["payload"].get("stop_receipt") is not None


def test_handoff_and_chat_stop_are_written_into_the_transcript():
    harness = logged_in()
    harness.send(YELLOW)
    handoff = harness.call("handoff")
    user, reply = harness.ws.transcript[-2:]
    assert (user.role, user.marker, user.hint) == ("user", "handoff", None)
    assert reply.rid == harness.ws.rkeys[handoff] == harness.ws.current["domestic"]
    assert harness.session.response(reply.rid)["intent"] == "handoff"
    stop = harness.call("chat_stop")
    assert harness.ws.transcript[-2].marker == "cancel"
    assert harness.session.response(harness.ws.rkeys[stop])["intent"] == "stop"


def test_stop_all_clears_request_keys_but_keeps_their_sequence():
    harness = logged_in()
    tkey = "T" + harness.send(RED)
    harness.confirm(tkey)
    harness.mfa_request(tkey)
    first = harness.ws.request_id("investment", ["net_return", None])
    assert harness.ws.request_id("investment", ["net_return", None]) == first
    sequence = harness.ws.request_seq
    harness.call("stop_all")
    assert harness.ws.requests == {} and harness.ws.request_seq == sequence
    assert harness.ws.request_id("investment", ["net_return", None]) != first
    assert inbox(harness) == [("expired", False)]
    turn = harness.ws.transcript[-1]
    assert (turn.role, turn.marker, turn.rid) == ("system", "stop_all", None)
    assert harness.ws.business["stop_results"][turn.id]["status"] in {
        "interrupted",
        "no_active_plan",
    }
    assert harness.step(tkey) == "withdrawn" and harness.ws.gestures == {}
    assert harness.session.response(harness.ws.tasks[tkey].rid)["status"] == "withdrawn"


def test_a_birthday_plan_then_refund_through_the_compensation_task(monkeypatch):
    harness = logged_in()
    tkey = "T" + harness.send(BIRTHDAY)
    harness.nodes(tkey, ["n0", "n1", "n2", "n3"])
    assert harness.confirm(tkey) == {"status": "settled"}
    harness.send("停止")
    version = tasks.list_version(harness.ws, "compensation", [tasks.compensation_plan(harness.ws)])
    harness.call("compensate_latest", version)
    compensation = harness.ws.business["compensation"]
    assert compensation.get("pending_id") and compensation.get("card")
    business = [task for task in harness.ws.tasks.values() if task.source == "compensation"]
    assert len(business) == 1 and business[0].key == "TB1"
    assert harness.listed()[0] == "TB1" or "TB1" in harness.listed()
    assert harness.step("TB1") == "confirm_intent"
    spy = Spy(harness.controller, monkeypatch)
    result = harness.confirm("TB1")
    assert result["status"] in {"settled", "waiting_mfa"}
    assert spy.counts["confirm_plan"] == 1


def test_select_nodes_is_a_plan_only_registration():
    harness = logged_in()
    yellow = "T" + harness.send(YELLOW)
    refused("not_applicable", harness.call, "select_nodes", yellow, "0" * 32, [])
    tkey = "T" + harness.send(BIRTHDAY)
    version = tasks.nodes_version(harness.ws, harness.ws.tasks[tkey])
    refused("card_changed", harness.call, "select_nodes", tkey, "0" * 32, ["n0"])
    refused("not_found", harness.call, "select_nodes", tkey, version, ["n4"])
    refused("bad_json", harness.call, "select_nodes", tkey, version, ["n0", "n0"])
    refused("not_found", harness.call, "select_nodes", "TR9", version, ["n0"])
    harness.call("select_nodes", tkey, version, [])
    assert harness.ws.selection[tkey] == [] and harness.step(tkey) == "select_nodes"


def test_a_device_gesture_leaves_other_actions_untouched(monkeypatch):
    harness = logged_in()
    other = "T" + harness.send(RED)
    harness.confirm(other)
    current = "T" + harness.send(YELLOW)
    guarded = protected(harness.controller, harness.identity(other))
    harness.confirm(current)
    after = protected(harness.controller, harness.identity(other))
    assert {key: after[key] for key in ("action", "cert", "card", "display_card", "challenge")} == {
        key: guarded[key] for key in ("action", "cert", "card", "display_card", "challenge")
    }
    assert harness.step(other) == "not_current"
    snapshot(harness.controller, harness.session)


def test_a_business_redisplay_moves_the_task_and_its_page_slot(monkeypatch):
    """app::approve_saved: a redisplayed business card replaces the stored pending."""
    harness = logged_in()
    harness.call("subscriptions_query")
    (tkey,) = harness.listed()
    old = harness.identity(tkey)
    assert harness.ws.business["subscription_pending"]["pending_id"] == old
    old_token = harness.ws.gestures[tkey].token
    approve = harness.controller.workflow._approve
    drift = {"left": 1}

    def changed(gateway, prepared, expected, display_id):
        if drift["left"]:
            drift["left"] -= 1
            prepared.proposal.card["drift"] = True
        return approve(gateway, prepared, expected, display_id)

    monkeypatch.setattr(harness.controller.workflow, "_approve", changed)
    assert harness.confirm(tkey) == {"status": "redisplay"}
    new = harness.identity(tkey)
    assert new != old and harness.ws.business["subscription_pending"]["pending_id"] == new
    assert harness.step(tkey) == "confirm_intent"
    assert harness.ws.gestures[tkey].token != old_token
    assert harness.confirm(tkey) == {"status": "settled"}


def test_an_aa_completion_is_a_new_reply_with_its_own_words():
    """P1 B12: the chosen participants make a new reply; its words are fixed when sent."""
    harness = logged_in()
    source = harness.send("这顿饭460我们四个AA")
    assert "T" + source not in harness.ws.tasks
    with pytest.raises(errors.WebError) as caught:
        harness.call("complete_aa", source, ["c0"])
    assert caught.value.code == "invalid_input"
    created = harness.call("complete_aa", source, ["c0", "c1", "c2"])
    assert created != source and harness.ws.current["domestic"] == harness.ws.rkeys[created]
    turn = harness.ws.turn_of(created)
    assert turn.hint and turn.shortcut is None
    tkey = "T" + created
    assert harness.step(tkey) == "confirm_intent"
    assert harness.confirm(tkey) == {"status": "settled"}
    harness.call("make_current", source)
    assert harness.call("complete_aa", source, ["c0", "c1", "c2"]) == created
