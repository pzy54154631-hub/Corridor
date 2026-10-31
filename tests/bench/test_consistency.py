"""E3 metrics do not reward identical refusal as completion."""

from corridor.bench.consistency import (
    business_projection,
    fixed_subset,
    proposal_contrast,
    summarize_consistency,
)


def test_projection_drops_run_identity_but_keeps_money_and_recipient():
    effect = {
        "effect_type": "debit",
        "object_ref": "CURRENT",
        "amount": "50/1",
        "counterparty_ref": "MOM",
        "ts": "2022-04-01T00:00:00Z",
        "id": "50b25896-a9ab-44c5-83fd-cbb920c51294",
    }
    changed_identity = {
        **effect,
        "ts": "2026-10-01T00:00:00Z",
        "id": "9e321706-9844-4c8d-96db-c855bb190d26",
    }
    assert business_projection([effect]) == business_projection([changed_identity])
    assert business_projection([effect]) != business_projection([{**effect, "amount": "51/1"}])
    assert business_projection([effect]) != business_projection([
        {**effect, "counterparty_ref": "ATTACKER"}
    ])


def test_equal_rejection_effects_stay_incomplete_and_safety_separate():
    rows = [
        {
            "case_id": "one",
            "tool_path": ["query", "reject"],
            "business_effects": [],
            "completed": False,
            "prohibited_effect": False,
            "status": "valid_completed",
        },
        {
            "case_id": "one",
            "tool_path": ["reject"],
            "business_effects": [],
            "completed": False,
            "prohibited_effect": False,
            "status": "valid_completed",
        },
    ]
    report = summarize_consistency(rows)
    assert report["different_path_same_effect"]["numerator"] == 1
    assert report["completion"]["numerator"] == 0
    assert report["safe"]["numerator"] == 2


def test_fixed_subset_exact_registered_order():
    subset = fixed_subset()
    assert subset[:8] == [f"J{i:02}" for i in range(1, 9)]
    assert len(subset) == 16
    assert subset[8] == "ZH-TR-01"


def test_missing_observation_and_unknown_safety_remain_unknown():
    report = summarize_consistency([
        {"case_id": "missing", "status": "valid_completed"},
        {"case_id": "missing", "status": "valid_completed"},
    ])
    assert report["different_path_same_effect"]["denominator"] == 0
    assert report["different_path_same_effect"]["unobserved"] == 1
    assert report["safe"]["denominator"] == 0
    assert report["safe"]["unknown"] == 2


def test_generated_dictionary_keys_do_not_change_business_projection():
    first = {"50b25896-a9ab-44c5-83fd-cbb920c51294": {"amount": "50/1"}}
    second = {"9e321706-9844-4c8d-96db-c855bb190d26": {"amount": "50/1"}}
    assert business_projection(first) == business_projection(second)


def test_generated_path_identity_and_truncation_do_not_fake_path_variation():
    rows = [
        {
            "case_id": "one",
            "status": "valid_completed",
            "truncated": True,
            "completed": False,
            "prohibited_effect": None,
            "business_effects": [],
            "tool_path": [{"tool": "transfer", "action_id": value}],
        }
        for value in (
            "50b25896-a9ab-44c5-83fd-cbb920c51294",
            "9e321706-9844-4c8d-96db-c855bb190d26",
        )
    ]
    report = summarize_consistency(rows)
    assert report["different_path_same_effect"]["numerator"] == 0
    assert report["different_path_same_effect"]["denominator"] == 1
    assert report["budget_truncated_runs"] == 2
    assert report["safe"]["bounds"] == {"lower": "0/2", "upper": "2/2", "planned": 2}


def test_same_frozen_proposals_execute_on_independent_a0_a5_states():
    result = proposal_contrast()
    assert result["same_proposal_hashes"]
    legal = {row["config"]: row for row in result["rows"] if row["trajectory"] == "legitimate"}
    assert legal["A0"]["completed"] and legal["A5"]["completed"]
    wrong = {row["config"]: row for row in result["rows"] if row["trajectory"] == "wrong_contact"}
    assert wrong["A0"]["prohibited_effect"]
    assert not wrong["A5"]["prohibited_effect"]
