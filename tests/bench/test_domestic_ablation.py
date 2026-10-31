"""Prerequisite checks for the domestic experimental executor."""

from pathlib import Path

from corridor.bench.ablation import (
    CONFIGS,
    MANIFEST,
    TransferExperiment,
    configure_experiment,
    execute_prepared,
    validate_manifest,
)
from corridor.bench.bank_cases import BankCase, write_json
from corridor.bench.planning_runner import prepare_transfer
from corridor.crypto.jcs import H
from corridor.schemas.base import loads_exact
from corridor.sim.fixtures import make_state


def test_manifest_is_total_and_disjoint() -> None:
    assert validate_manifest() == []
    assert len(CONFIGS) == 10


def test_manifest_rejects_missing_and_duplicate_checks(tmp_path: Path) -> None:
    body = loads_exact(MANIFEST.read_bytes())
    missing = {**body, "checks": body["checks"][1:]}
    path = tmp_path / "manifest.json"
    write_json(path, missing)
    assert "unmapped_or_stale_checks" in validate_manifest(path)
    write_json(path, {**body, "checks": [*body["checks"], body["checks"][0]]})
    assert "duplicate_mapping" in validate_manifest(path)


def test_repeated_baseline_calls_preserve_bank_effect_history() -> None:
    case = BankCase(make_state(), task_id="CACHE", subcase_id="A0")
    execute_prepared("A0", case, prepare_transfer(case, "200/1", step=0))
    second = execute_prepared("A0", case, prepare_transfer(case, "200/1", step=1))
    assert second["final_state"]["balances"]["A_CURRENT"] == "9600/1"
    assert len(second["bank_effects"]) == 2


def test_partial_preparation_cannot_apply_masked_production_rejections() -> None:
    case = BankCase(make_state(), task_id="PREP", subcase_id="A2")
    configure_experiment(case, "A2")
    prepared = prepare_transfer(case, "200/1", step=0)
    prepared.cert = prepared.cert.model_copy(update={"derivations": {}})
    for _ in range(3):
        case.gateway.propose(prepared.action, prepared.cert)
    assert case.state.security.reject_streak == {}
    assert case.state.security.locked_at == {}
    assert case.state.security.approval_requests == {}
    result = execute_prepared("A2", case, prepared)
    assert result["status"] == "settled"
    assert result["experiment_state"]["rejected_proposals"] == 0


def test_declined_confirmation_never_mints_mfa() -> None:
    for config in ("A2", "A5"):
        case = BankCase(make_state(), task_id="DECLINE", subcase_id=config)
        configure_experiment(case, config)
        prepared = prepare_transfer(case, "1500/1", step=0)
        result = execute_prepared(config, case, prepared, approve=False, allow_mfa=True)
        assert result["status"] in {"rejected", "waiting_mfa"}
        assert result["mfa_attempted"] is False
        assert case.state.security.mfa_grants == {}
        assert result["bank_effects"] == []


def test_two_transfer_turns_use_current_private_ledger() -> None:
    for config in ("A2", "A3", "A4", "A5"):
        case = BankCase(make_state(), task_id="TWO", subcase_id=config)
        configure_experiment(case, config)
        for step in range(2):
            result = execute_prepared(config, case, prepare_transfer(case, "600/1", step=step))
            assert result["status"] == "settled", (config, result)
        assert result["final_state"]["balances"]["A_CURRENT"] == "8800/1"
        assert len(result["bank_effects"]) == 2


def test_invalid_certificate_never_reaches_bank_or_consumes_nonce() -> None:
    case = BankCase(make_state(), task_id="INVALID", subcase_id="A4")
    configure_experiment(case, "A4")
    prepared = prepare_transfer(case, "200/1", step=0)
    prepared.cert = prepared.cert.model_copy(update={"action_hash": "0" * 64})
    result = execute_prepared("A4", case, prepared)
    assert result["status"] == "rejected"
    assert result["bank_effects"] == []
    assert result["experiment_state"]["consumed_nonces"] == []


def test_masked_source_does_not_suppress_missing_mfa_or_mutate_origin() -> None:
    case = BankCase(make_state(), task_id="W5", subcase_id="masked-source")
    prepared = prepare_transfer(case, "1500/1", step=0)
    prepared.cert = prepared.cert.model_copy(update={"derivations": {}})
    before = H(case.state)
    engine = TransferExperiment(case, "A2")
    result = engine.execute(prepared, approve=True, allow_mfa=False)
    assert result["status"] == "waiting_mfa"
    assert "TIER-MFA-REQUIRED" in result["rule_ids"]
    assert result["experiment_state"]["rejected_proposals"] == 0
    assert result["experiment_state"]["consumed_nonces"] == []
    assert result["experiment_state"]["approval_consumed"] == 0
    assert H(case.state) == before


def test_masked_source_does_not_leave_rejections_on_success() -> None:
    case = BankCase(make_state(), task_id="W5b", subcase_id="masked-source")
    prepared = prepare_transfer(case, "200/1", step=0)
    prepared.cert = prepared.cert.model_copy(update={"derivations": {}})
    # The confirmation is deliberately made against the current canonical card.
    engine = TransferExperiment(case, "A2")
    result = engine.execute(prepared)
    assert result["status"] == "settled"
    assert result["experiment_state"]["rejected_proposals"] == 0
    assert result["experiment_state"]["approval_consumed"] == 1


def test_a0_wrong_recipient_reaches_real_bank_and_a5_blocks() -> None:
    for config in ("A0", "A5"):
        case = BankCase(make_state(), task_id="W2", subcase_id=config)
        prepared = prepare_transfer(case, "200/1", step=0)
        prepared.action = prepared.action.model_copy(
            update={
                "args": prepared.action.args.model_copy(update={"contact_ref": "CONTACT_LIN_A"})
            }
        )
        result = execute_prepared(config, case, prepared)
        assert result["status"] == ("settled" if config == "A0" else "rejected")
        assert bool(result["bank_effects"]) == (config == "A0")


def test_supported_a5_projection_matches_experimental_full_transfer() -> None:
    case = BankCase(make_state(), task_id="W1", subcase_id="parity")
    prepared = prepare_transfer(case, "200/1", step=0)
    experiment = TransferExperiment(case, "A5").execute(prepared)
    production = execute_prepared("A5", case, prepared)
    assert experiment["status"] == production["status"] == "settled"
    assert experiment["final_state"]["balances"] == production["final_state"]["balances"]
    assert (
        experiment["bank_effects"][0]["exact_effects"]
        == production["bank_effects"][0]["exact_effects"]
    )
