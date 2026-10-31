"""Operation coverage derives from public goals, never from frozen blind cases."""

from pathlib import Path

import pytest

from corridor.bench.ablation import CONFIGS, TransferExperiment
from corridor.bench.witness.operations import (
    BANK_WRITE_OPERATIONS,
    OPERATION_CATEGORIES,
    operation_case,
    run_operation_case,
    run_operation_matrix,
)


@pytest.mark.parametrize("kind", BANK_WRITE_OPERATIONS)
def test_every_supported_write_has_real_positive_and_negative_witness(kind: str) -> None:
    legal = run_operation_case(kind, "A5", attack=False)
    weak = run_operation_case(kind, "A3", attack=True)
    strong = run_operation_case(kind, "A5", attack=True)
    assert legal["status"] == "settled", legal
    assert legal["oracle"]["completed"], legal
    assert weak["status"] == "settled", weak
    assert weak["oracle"]["prohibited_effect"], weak
    assert strong["status"] == "rejected", strong
    assert strong["oracle"]["prohibited_effect"] is False, strong
    assert weak["input_hash"] == strong["input_hash"]


@pytest.mark.parametrize("kind", BANK_WRITE_OPERATIONS)
def test_selected_full_checks_match_production_effects(kind: str) -> None:
    case, prepared = operation_case(kind, "A5")
    experimental = TransferExperiment(case, "A5").execute(prepared)
    production = run_operation_case(kind, "A5", attack=False)
    assert experimental["status"] == production["status"] == "settled"
    assert experimental["bank_effects"] == production["bank_effects"]


@pytest.mark.parametrize("config", ("A0", "A2", "A3", "A4"))
def test_card_effects_update_the_private_projection_before_next_turn(config: str) -> None:
    from corridor.bench.ablation import execute_prepared
    from corridor.bench.card_runner import prepare_card

    case, prepared = operation_case("freeze_card", config)
    first = execute_prepared(config, case, prepared)
    assert first["status"] == "settled"
    assert case.state.cards["CARD_DEMO"].status == "frozen"
    next_action = prepare_card(case, "unfreeze_card", change={"status": "active"}, step=2)
    result = execute_prepared(config, case, next_action)
    assert result["status"] == "settled", result
    assert case.state.cards["CARD_DEMO"].status == "active"
    assert len(result["bank_effects"]) == 2


def test_missing_card_source_cannot_hide_retained_authority_checks() -> None:
    case, prepared = operation_case("raise_card_limit", "A2")
    prepared.cert = prepared.cert.model_copy(update={"derivations": {}})
    prepared.action = prepared.action.model_copy(update={"intent_id": case.intent(99)})
    result = TransferExperiment(case, "A2").execute(prepared, allow_mfa=False)
    assert "AUTH-SCOPE" in result["rule_ids"]
    assert result["bank_effects"] == []
    assert result["experiment_state"]["consumed_nonces"] == []


@pytest.mark.parametrize("config", ("A0", "A3"))
def test_reobserved_bank_receipt_does_not_reapply_state_effects(config: str) -> None:
    from corridor.bench.ablation import execute_prepared
    from corridor.kernel.binding import dependency_hashes

    case, prepared = operation_case("transfer", config)
    first = execute_prepared(config, case, prepared)
    prepared.cert = prepared.cert.model_copy(
        update={"dependency_hashes": dependency_hashes(prepared.action, prepared.cert, case.state)}
    )
    second = execute_prepared(config, case, prepared)
    assert first["status"] == second["status"] == "settled"
    assert len(second["bank_effects"]) == 1
    assert len(case.state.ledger) == 1


def test_operation_matrix_accounts_for_every_public_goal_and_extra(tmp_path: Path) -> None:
    report = run_operation_matrix(tmp_path)
    assert set(report["coverage"]) == set(OPERATION_CATEGORIES)
    assert report["passed"]
    for kind, item in report["coverage"].items():
        assert item["evaluated"] + item["not_evaluable"] == 2 * len(CONFIGS)
        if kind in BANK_WRITE_OPERATIONS:
            assert item["evaluated"] == 2 * len(CONFIGS)
            assert item["negative_witness_passed"]
        else:
            assert item["reason"]


def test_schedule_and_aa_write_effects_are_observable_without_bank_receipts() -> None:
    for kind in ("schedule_transfer", "aa_request"):
        legal = run_operation_case(kind, "A5", attack=False)
        weak = run_operation_case(kind, "A3", attack=True)
        strong = run_operation_case(kind, "A5", attack=True)
        assert legal["oracle"]["completed"], legal
        assert weak["oracle"]["prohibited_effect"], weak
        assert strong["oracle"]["prohibited_effect"] is False, strong
        assert strong["status"] in {"rejected", "waiting_mfa"}, strong
        assert weak["input_hash"] == strong["input_hash"]
        assert weak["receipt"] is None
        assert weak["gateway_ledger"]
        assert all(event["effect_origin"] == "ledger_projection" for event in weak["bank_effects"])


@pytest.mark.parametrize("kind", ("schedule_transfer", "aa_request"))
def test_local_write_builders_match_production_payloads(kind: str) -> None:
    case, prepared = operation_case(kind, "A5")
    experimental = TransferExperiment(case, "A5").execute(prepared)
    production = run_operation_case(kind, "A5", attack=False)
    assert experimental["status"] == production["status"]
    assert experimental["analysis_receipt"] == production["analysis_receipt"]
    assert [row["exact_effects"] for row in experimental["bank_effects"]] == [
        row["exact_effects"] for row in production["bank_effects"]
    ]
