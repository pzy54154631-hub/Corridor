"""Domestic breaker signals retain the existing account-wide safety controls."""

from fractions import Fraction

import pytest

from corridor.crypto.jcs import H, jcs_bytes
from corridor.gateway.audit import AuditLog
from corridor.gateway.service import Gateway
from corridor.stores.bank import add_seconds
from tests.gateway_cases import gateway_case


def snapshot(gateway, action):
    return gateway.safety_snapshot(action.principal)


def test_money_rejections_transition_through_watch_without_resetting_legacy_lock():
    gateway, _, action, cert = gateway_case()
    assert snapshot(gateway, action)["state"] == "Normal"
    bad = cert.model_copy(update={"action_hash": H("wrong")})
    for index in range(3):
        gateway.propose(action, bad, request_id="SAME")
        state = snapshot(gateway, action)
        assert state["consecutive_money_rejections"] == index + 1
        assert state["state"] == ("Locked" if index == 2 else "Watch")
    transitions = [
        (row["payload"]["from"], row["payload"]["to"])
        for row in gateway.audit.records()
        if row["event"] == "safety_transition"
    ]
    assert transitions == [("Normal", "Watch"), ("Watch", "Locked")]
    assert gateway.store.snapshot().security.reject_streak[action.principal] == 3


def test_accepted_money_proposal_resets_only_the_new_consecutive_counter():
    gateway, _, action, cert = gateway_case()
    bad = cert.model_copy(update={"action_hash": H("wrong")})
    gateway.propose(action, bad, request_id="SAME")
    gateway.propose(action, cert, request_id="SAME")
    assert snapshot(gateway, action)["consecutive_money_rejections"] == 0
    assert gateway.store.snapshot().security.reject_streak[action.principal] == 1


@pytest.mark.rule("BRK-LOCK")
def test_two_injection_hits_lock_yellow_actions_and_record_rule():
    gateway, _, action, cert = gateway_case()
    first = gateway.propose(action, cert, request_id="SAME", injection_flag=True)
    assert first.verdict.tier == "yellow"
    assert snapshot(gateway, action)["state"] == "Watch"
    second = gateway.propose(action, cert, request_id="SAME", injection_flag=True)
    assert second.verdict.outcome == "Reject"
    assert "BRK-LOCK" in second.verdict.rule_ids
    assert "BRK-LOCK" in gateway.dispatch(action, cert).rule_ids
    assert snapshot(gateway, action)["injection_hits_10m"] == 2
    assert gateway.bank.execute_count == 0


@pytest.mark.parametrize("seconds,locked", [(599, True), (600, True), (601, False)])
def test_injection_window_uses_simulated_clock(seconds, locked):
    gateway, _, action, cert = gateway_case()
    gateway.propose(action, cert, request_id="SAME", injection_flag=True)
    gateway.store.update(now=add_seconds(gateway.store.snapshot().now, seconds))
    gateway.propose(action, cert, request_id="SAME", injection_flag=True)
    assert (snapshot(gateway, action)["state"] == "Locked") is locked


@pytest.mark.rule("BRK-LOCK")
def test_three_failed_mfa_attempts_lock_red_actions_without_logging_secrets():
    gateway, _, action, cert = gateway_case(Fraction(1500))
    gateway.propose(action, cert)
    challenge = gateway.request_mfa(action, cert)
    verifier = gateway.mfa
    code = verifier.test_code(challenge)
    wrong = "000000" if code != "000000" else "111111"
    for index in range(3):
        assert verifier.verify(challenge, wrong, now=gateway.store.snapshot().now) is None
        assert snapshot(gateway, action)["consecutive_mfa_failures"] == index + 1
    assert "BRK-LOCK" in gateway.dispatch(action, cert).rule_ids
    assert gateway.bank.execute_count == 0
    audit = jcs_bytes(gateway.audit.records())
    assert jcs_bytes(code) not in audit and jcs_bytes(wrong) not in audit


def test_mfa_success_resets_streak_but_another_challenge_does_not():
    gateway, _, action, cert = gateway_case(Fraction(1500))
    gateway.propose(action, cert)
    challenge = gateway.request_mfa(action, cert)
    verifier, now = gateway.mfa, gateway.store.snapshot().now
    for _ in range(2):
        assert verifier.verify(challenge, "wrong", now=now) is None
    assert snapshot(gateway, action)["consecutive_mfa_failures"] == 2
    assert verifier.verify(challenge, verifier.test_code(challenge), now=now) is not None
    assert snapshot(gateway, action)["consecutive_mfa_failures"] == 0
    for index in range(3):
        other = verifier.challenge(
            principal=action.principal,
            device_session_id=action.device_session_id,
            scope_hash=H({"test_scope": index}),
            now=now,
            sequence=100 + index,
        )
        assert verifier.verify(other, "wrong", now=now) is None
    assert snapshot(gateway, action)["consecutive_mfa_failures"] == 3
    assert snapshot(gateway, action)["state"] == "Locked"


def test_mfa_failure_count_survives_gateway_restart(tmp_path):
    gateway, _, action, cert = gateway_case(Fraction(1500), path=tmp_path)
    gateway.propose(action, cert)
    challenge = gateway.request_mfa(action, cert)
    assert gateway.mfa.verify(challenge, "wrong", now=gateway.store.snapshot().now) is None
    gateway = Gateway(
        gateway.store, gateway.bank, audit=AuditLog(tmp_path / "audit.jsonl"), mfa=gateway.mfa
    )
    for _ in range(2):
        assert gateway.mfa.verify(challenge, "wrong", now=gateway.store.snapshot().now) is None
    assert snapshot(gateway, action)["consecutive_mfa_failures"] == 3
    assert "BRK-LOCK" in gateway.dispatch(action, cert).rule_ids


def test_watch_survives_gateway_restart_and_lock_survives_relogin(tmp_path):
    gateway, device, action, cert = gateway_case(path=tmp_path)
    gateway.propose(action, cert, request_id="SAME", injection_flag=True)
    gateway = Gateway(
        gateway.store, gateway.bank, audit=AuditLog(tmp_path / "audit.jsonl"), mfa=gateway.mfa
    )
    assert snapshot(gateway, action)["state"] == "Watch"
    gateway.propose(action, cert, request_id="SAME", injection_flag=True)
    login = device.login(
        session_id="NEW_SESSION", expires_at=add_seconds(gateway.store.snapshot().now, 3600)
    )
    assert gateway.login(login).status == "accepted"
    assert snapshot(gateway, action)["state"] == "Locked"


def test_unlock_resets_breaker_but_keeps_old_plans_void():
    gateway, device, action, cert = gateway_case()
    for _ in range(2):
        gateway.propose(action, cert, request_id="SAME", injection_flag=True)
    gateway.store.update(now=add_seconds(gateway.store.snapshot().now, 1800))
    scope = gateway.unlock_scope(action.principal)
    confirmation = device.confirm_input(
        intent_id=H(scope), slot="unlock", value=scope, now=gateway.store.snapshot().now
    )
    challenge = gateway.request_unlock_mfa(action.principal)
    grant = device.complete_mfa(gateway.mfa, challenge, now=gateway.store.snapshot().now)
    assert gateway.unlock(action.principal, confirmation, grant).status == "unlocked"
    assert snapshot(gateway, action)["state"] == "Normal"
    assert snapshot(gateway, action)["injection_hits_10m"] == 0
    assert gateway.dispatch(action, cert).rule_ids == ["APR-VOID"]


@pytest.mark.parametrize("task_id", ["J-BRK-01", "J-BRK-02", "J-BRK-03"])
def test_independent_breaker_demonstrations_are_structurally_judged(task_id, tmp_path):
    from corridor.bench.breaker_runner import run_breaker

    result = run_breaker(task_id, evidence_dir=tmp_path)
    assert result["passed"], result["checks"]
    assert result["planner_mode"] == "scripted"
    assert result["checks"]["green_query_available"]
    assert result["checks"]["signed_unlock"]
