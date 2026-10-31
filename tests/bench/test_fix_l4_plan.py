"""Synthetic regressions for rejected plan registration across shared runners."""

from dataclasses import replace

import pytest

from corridor.bench.ablation import CONFIGS, configure_experiment, execute_prepared
from corridor.bench.agentdojo_gate import NativeTransferGate
from corridor.bench.bank_cases import BankCase
from corridor.bench.planning_runner import prepare_transfer
from corridor.gateway.service import Gateway, GatewayResult
from corridor.sim.fixtures import make_state


def reject_plan(*_args, **_kwargs):
    return GatewayResult("rejected", ["PLAN-SCOPE"])


def test_registration_rejection_is_a_prepared_defense_result(monkeypatch):
    case = BankCase(make_state(), task_id="SYNTHETIC-PLAN", subcase_id="reject")
    monkeypatch.setattr(case.gateway, "submit_plan", reject_plan)
    prepared = prepare_transfer(case, "20/1", step=0)
    assert prepared.plan_rejection.rule_ids == ["PLAN-SCOPE"]
    assert prepared.proposal.verdict.outcome == "Reject"
    assert prepared.proposal.verdict.rule_ids == ["PLAN-SCOPE"]
    assert case.dispatch(prepared).rule_ids == ["PLAN-SCOPE"]
    assert case.state.ledger == case.initial.ledger


def test_judge_keeps_registration_rejection_in_its_expected_observations(monkeypatch):
    from corridor.agent.llm import LLMClient
    from corridor.bench.judge_runner import JudgeRun

    run = JudgeRun("J-SYNTHETIC", LLMClient(), None)
    case = run.case("registration-rejection")
    monkeypatch.setattr(case.gateway, "submit_plan", reject_plan)
    prepared = prepare_transfer(case, "20/1", step=0)
    outcome = case.dispatch(prepared)
    run.expect(case, "registration_rule", outcome.rule_ids, ["PLAN-SCOPE"])
    run.expect(case, "registration_status", outcome.status, "rejected")
    assert run.finish()["passed"] is True


@pytest.mark.parametrize("config", list(CONFIGS) + ["B_prompt", "B_guard"])
def test_setup_rejection_is_not_evaluable_for_every_configuration(monkeypatch, config):
    case = BankCase(make_state(), task_id="SYNTHETIC-SETUP", subcase_id=config)
    monkeypatch.setattr(case.gateway, "submit_plan", reject_plan)
    prepared = replace(prepare_transfer(case, "20/1", step=0), preparation_role="setup")
    result = execute_prepared(config, case, prepared)
    assert result["status"] == "not_evaluable"
    assert result["reason"] == "unsupported_action_target_mapping"
    assert result["rule_ids"] == ["PLAN-SCOPE"]
    assert result["actual_actions"] == []
    assert case.dispatch(prepared).status == "not_evaluable"


@pytest.mark.parametrize("config", list(CONFIGS) + ["B_prompt", "B_guard"])
def test_setup_uses_full_registration_even_when_plan_layer_is_ablated(config):
    case = BankCase(make_state(), task_id="SYNTHETIC-SETUP-SCOPE", subcase_id=config)
    configure_experiment(case, config)
    original = prepare_transfer(case, "20/1", step=0)
    terms = {key.removeprefix("args."): value for key, value in original.cert.derivations.items()}
    terms["source_ref"] = terms["source_ref"].model_copy(update={"version": 99})
    prepared = case.prepare(
        "transfer",
        original.action.args.model_dump(mode="json"),
        terms,
        authorization=case.state.authorizations[original.cert.authorization_ref],
        preparation_role="setup",
    )
    result = execute_prepared(config, case, prepared)
    assert result["status"] == "not_evaluable"
    assert result["reason"] == "unsupported_action_target_mapping"
    assert any(rule.startswith("PLAN-") for rule in result["rule_ids"])


@pytest.mark.parametrize("config", list(CONFIGS))
def test_plan_checks_are_selected_before_registration(monkeypatch, config):
    import corridor.bench.ablation as ablation

    case = BankCase(make_state(), task_id="SYNTHETIC-SELECT", subcase_id=config)
    configure_experiment(case, config)
    if config == "A5":
        monkeypatch.setattr(case.gateway, "submit_plan", reject_plan)
    else:

        def check_plan(*_args, **_kwargs):
            assert "L-prov" in CONFIGS[config], "Disabled plan check ran"
            return "PLAN-SCOPE"

        monkeypatch.setattr(ablation, "validate_plan", check_plan)
    prepared = prepare_transfer(case, "20/1", step=0)
    result = execute_prepared(config, case, prepared)
    if "L-prov" in CONFIGS[config]:
        assert result["status"] == "rejected"
        assert result["rule_ids"] == ["PLAN-SCOPE"]
        assert result["actual_actions"] == []
    else:
        assert prepared.plan_rejection is None
        assert result["status"] == "settled"
        assert not result["rule_ids"]


def test_native_gate_keeps_plan_rejection_as_a_defense(monkeypatch):
    environment = {
        "bank_account": {
            "balance": "1000",
            "iban": "SOURCE",
            "transactions": [
                {"sender": "me", "recipient": "KNOWN", "amount": "20", "date": "2022-04-01"}
            ],
            "scheduled_transactions": [],
        }
    }
    gate = NativeTransferGate("Pay KNOWN 20", environment)
    monkeypatch.setattr(Gateway, "submit_plan", reject_plan)
    result = gate.evaluate("send_money", {"recipient": "KNOWN", "amount": "20"})
    assert result["allow"] is False
    assert result["reason"] == "native_plan_reject"
    assert result["rule_ids"] == ["PLAN-SCOPE"]
    assert result["mirror_balance"] == "1000/1"


@pytest.mark.parametrize("runner", ["shared", "E2", "E3"])
def test_common_runners_send_rejected_proposals_to_oracle(monkeypatch, tmp_path, runner):
    from corridor.agent.llm import LLMClient
    from corridor.bench.blind_zh import run_case
    from corridor.bench.consistency import run_consistency_episode
    from corridor.bench.interaction import _common_case, run_interaction_episode

    original_prepare = BankCase.prepare

    def reject_candidate_plan(case, *args, **kwargs):
        if kwargs.get("preparation_role") == "setup":
            return original_prepare(case, *args, **kwargs)
        with monkeypatch.context() as scoped:
            scoped.setattr(case.gateway, "submit_plan", reject_plan)
            return original_prepare(case, *args, **kwargs)

    monkeypatch.setattr(BankCase, "prepare", reject_candidate_plan)
    if runner == "shared":
        case = _common_case({"id": "ZH-TR-01", "request": {"task": "synthetic transfer"}})
        result = run_case(case, client=LLMClient(), config="A5")
    elif runner == "E2":
        result = run_interaction_episode("ZH-TR-01", LLMClient(), "A5", 1, tmp_path)
    else:
        result = run_consistency_episode(
            "ZH-TR-01", client=LLMClient(), config="A5", seed=1, output=tmp_path
        )
    assert result["status"] == "valid_completed"
    assert result["error_class"] is None
    assert result["steps"][0]["status"] == "rejected"
    assert result["steps"][0]["rule_ids"] == ["PLAN-SCOPE"]
    assert "oracle" in result


@pytest.mark.parametrize("config", list(CONFIGS) + ["B_prompt", "B_guard"])
@pytest.mark.parametrize("kind", ["transfer", "purchase_product", "bill_analyze"])
@pytest.mark.parametrize("rule", ["PLAN-SCOPE", "TOOL-UNAVAILABLE"])
def test_real_goal_setup_rejection_precedes_planner_for_every_configuration(
    monkeypatch, config, kind, rule
):
    from corridor.agent.llm import LLMClient
    from corridor.bench.blind_zh import run_case
    from corridor.bench.interaction import _common_case

    case = _common_case({"id": "ZH-TR-01", "request": {"task": "synthetic goal setup"}})
    case["legit_goal"]["kind"] = kind
    case["legit_goal"]["target_role"] = {
        "purchase_product": "P_A",
        "bill_analyze": "CURRENT",
        "transfer": "MOM",
    }[kind]
    client = LLMClient()
    monkeypatch.setattr(Gateway, "submit_plan", lambda *a, **kw: GatewayResult("rejected", [rule]))
    result = run_case(case, config=config, client=client)
    assert result["evaluability"] == "not_evaluable"
    assert result["reason"] == "unsupported_action_target_mapping"
    assert result["error_class"] is None
    assert result["steps"][0]["preparation_role"] == "setup"
    assert result["steps"][0]["rule_ids"] == [rule]
    assert client.calls == []
    assert result["metrics"]["tool_calls"] == 0


def test_goal_setup_ignores_kernel_domain_rejection():
    from corridor.bench.blind_zh import _goal_setup_rejection

    # This is representable, but exceeds the synthetic available balance.
    assert (
        _goal_setup_rejection({"kind": "transfer", "target_role": "MOM", "amount": "999999/1"})
        is None
    )


def test_goal_without_typed_kind_is_unsupported_setup():
    from corridor.bench.blind_zh import _goal_setup_rejection

    assert _goal_setup_rejection({"text": "Synthetic uncompiled goal"}) == []


@pytest.mark.parametrize("config", list(CONFIGS) + ["B_prompt", "B_guard"])
def test_inapplicable_mfa_fault_is_recorded_without_fabricating_injection(config):
    from corridor.agent.llm import LLMClient
    from corridor.bench.blind_zh import run_case
    from corridor.bench.interaction import _common_case

    case = _common_case({"id": "ZH-TR-01", "request": {"task": "synthetic small transfer"}})
    case["faults"] = [{"kind": "mfa_failure", "turn": 0, "expectation": "must_resume"}]
    row = run_case(case, config=config, client=LLMClient())
    assert row["error_class"] is None
    assert row["faults"][0]["injected"] is False
    assert row["fault_outcome"] == "not_injected"
    assert row["faults"][0]["reason"] in {
        "mfa_disabled_by_configuration",
        "mfa_not_applicable_to_proposal",
    }


def test_unavailable_mfa_challenge_is_not_an_apparatus_error(monkeypatch):
    from corridor.agent.llm import LLMClient
    from corridor.bench.blind_zh import run_case
    from corridor.bench.interaction import _common_case

    case = _common_case({"id": "ZH-TR-01", "request": {"task": "synthetic red transfer"}})
    case["legit_goal"]["amount"] = "1200/1"
    case["faults"] = [{"kind": "mfa_failure", "turn": 0, "expectation": "must_resume"}]
    monkeypatch.setattr(Gateway, "request_mfa", lambda *_args, **_kwargs: None)
    # Declined approval keeps ordinary execution from attempting a second MFA.
    import corridor.bench.blind_zh as blind_zh

    monkeypatch.setattr(
        blind_zh, "simulate_user", lambda *_args, **_kwargs: {"approve": False, "allow_mfa": False}
    )
    row = run_case(case, config="A5", client=LLMClient())
    assert row["error_class"] is None
    assert row["faults"][0]["injected"] is False
    assert row["faults"][0]["reason"] == "mfa_challenge_unavailable"
    assert row["fault_outcome"] == "not_injected"
