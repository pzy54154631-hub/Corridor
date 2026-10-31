"""Round-5 red-team reproducers for the domestic anomaly lockout state machine.

Each test states the rule it checks and FAILS on tag domestic-freeze-2.
"""

from corridor.crypto.jcs import H
from corridor.gateway.breaker import project
from corridor.kernel.binding import intent_key
from tests.gateway_cases import approve, gateway_case


def _transitions(gateway, principal):
    return [
        (row["payload"]["from"], row["payload"]["to"], row["payload"]["reason"])
        for row in gateway.audit.records()
        if row["event"] == "safety_transition" and row["principal_token"] == principal
    ]


def _audit_state(gateway, principal):
    state = gateway.store.snapshot()
    return project(
        gateway.audit.records(),
        principal=principal,
        session=state.security.active_device_sessions.get(principal),
        now=state.now,
    )["state"]


# breaker-mfa-01 ---------------------------------------------------------------
# Rule: safety-rules-bank.md "The append-only audit projects Normal -> Watch ->
# Locked"; breaker.py "Counters are recoverable"; safety_snapshot "structured,
# replayable safety state". An unknown-outcome lock (Gateway._unknown) sets
# security.locked_at without any `locked` / `safety_transition` audit record, so
# the audit-replayed state machine disagrees with the enforced lock, writes a
# Normal->Watch transition while the account is Locked, and later records a
# Locked->Normal unlock with no preceding entry into Locked.


def test_breaker_mfa_01_unknown_outcome_lock_is_missing_from_audited_state_machine():
    gateway, device, action, cert = gateway_case()
    principal = action.principal
    token = approve(gateway, device, action, cert)
    gateway.bank.faults[intent_key(action, gateway.store.snapshot())] = "response_loss"
    assert gateway.dispatch(action, cert, token).status == "unknown"
    assert principal in gateway.store.snapshot().security.locked_at
    assert gateway.safety_snapshot(principal)["state"] == "Locked"
    # A later rejected money proposal while the account is (enforced) Locked.
    bad = cert.model_copy(update={"action_hash": H("rejected-while-locked")})
    gateway.propose(action, bad, request_id="WHILE-LOCKED")
    transitions = _transitions(gateway, principal)
    assert _audit_state(gateway, principal) == "Locked", transitions
    assert ("Normal", "Watch", "money_rejected") not in transitions, transitions


def test_breaker_mfa_01b_birthday_unknown_demo_unlock_has_no_audited_lock_entry():
    from corridor.agent.domestic_session import DomesticSession
    from corridor.ui.domestic_demos import prepare_demo, unlock_recovery_demo

    demo = prepare_demo("birthday_unknown")
    controller = demo["controller"]
    gateway, principal = controller.gateway, controller.device.principal
    assert gateway.safety_snapshot(principal)["state"] == "Locked"
    # The headline recovery demo shows "Locked"; the audit projection must agree.
    assert _audit_state(gateway, principal) == "Locked", _transitions(gateway, principal)
    DomesticSession(controller).receive("stop")
    for key, event in {row.intent_key: row for row in controller.state.ledger}.items():
        if event.event == "unknown":
            assert gateway.reconcile(key).status == "settled"
    controller.advance_clock(gateway.safety_snapshot(principal)["unlock_after"])
    assert unlock_recovery_demo(demo)["status"] == "unlocked"
    transitions = _transitions(gateway, principal)
    # Every recorded exit from Locked must follow a recorded entry into Locked.
    current = "Normal"
    for source, target, _ in transitions:
        assert source == current, transitions
        current = target


# breaker-mfa-02 ---------------------------------------------------------------
# Rule: DESIGN-DOMESTIC 4.4 "同一会话内连续 N=3 次被拒的动钱提案" and "攻击者诱发的
# 锁死不算安全成功". In the domestic chat path a single device confirmation of one
# kernel-rejected transfer is proposed twice (Conversation.prepare and
# DeviceWorkflow._approve both call Gateway.propose), so one rejected proposal is
# counted as two consecutive money rejections and the second mistaken request
# locks the customer (voiding every in-flight plan) instead of the third.


def test_breaker_mfa_02_one_rejected_transfer_gesture_counts_as_one_rejection():
    from corridor.agent.domestic_session import DomesticSession
    from corridor.ui.controller import DemoController

    controller = DemoController()
    assert controller.start_session()["status"] == "ready"
    chat = DomesticSession(controller)
    principal = controller.device.principal
    first = chat.receive("给Chen转20000元", request_id="over-balance-1")
    result = chat.confirm(first["response_id"])
    assert "BAL-AVAILABLE" in result["rule_ids"]
    snapshot = controller.gateway.safety_snapshot(principal)
    assert snapshot["consecutive_money_rejections"] == 1, snapshot
    second = chat.receive("给Chen转15000元", request_id="over-balance-2")
    chat.confirm(second["response_id"])
    snapshot = controller.gateway.safety_snapshot(principal)
    # Two rejected user requests are fewer than N=3.
    assert snapshot["state"] != "Locked", snapshot
