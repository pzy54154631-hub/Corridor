"""FIX-L3 admission and development-only rehearsal regressions."""

from fractions import Fraction

from corridor.bench.live_zh import (
    BudgetJournal,
    SpendingJournal,
    build_plan,
    build_rehearsal_plan,
    comparison_groups,
    estimate_group_cost,
)
from scripts.run_live_zh import reserve_comparison_block


def test_case_comparison_contains_all_configs_variants_and_seeds():
    groups = comparison_groups(build_plan([{"case_id": "synthetic"}]))
    group = next(group for group in groups if group[0]["priority"] == 2)
    assert len(group) == 6
    assert {block["seed"] for block in group} == {1, 2, 3}
    assert {block["variant"] for block in group} == {"attack", "legit_twin"}
    assert sum(len(block["configs"]) for block in group) == 42
    assert estimate_group_cost(Fraction(1, 100), 42) == Fraction(63, 100)


def test_admission_uses_actual_spend_and_never_reserves_retries(tmp_path):
    plan = build_plan([{"case_id": "one"}, {"case_id": "two"}])
    groups = [group for group in comparison_groups(plan) if group[0]["priority"] == 2]
    journal = BudgetJournal(tmp_path / "budget.jsonl", "USD", Fraction(1))
    spending = SpendingJournal(tmp_path / "spending.jsonl", "USD", Fraction(1))
    first = reserve_comparison_block(groups[0], journal, spending, Fraction(1, 100))
    assert first["admitted"] is True
    assert first["reserved"] == "63/100"
    spending.begin("first-attempt", Fraction(1))
    spending.settle("first-attempt", Fraction(1, 10))
    second = reserve_comparison_block(groups[1], journal, spending, Fraction(1, 100))
    assert second["admitted"] is True  # Prior planning padding is not spent money.
    before = journal.path.read_bytes()
    assert reserve_comparison_block(groups[1], journal, spending, Fraction(1, 100)) == second
    assert journal.path.read_bytes() == before


def test_rehearsal_has_five_models_all_configs_both_variants_without_c2():
    plan = build_rehearsal_plan([{"case_id": "sample-a"}, {"case_id": "sample-b"}])
    rows = [dict(block, config=config) for block in plan for config in block["configs"]]
    development = [row for row in rows if row["experiment"] == "rehearsal"]
    assert len(development) == 5 * 8 * 2
    for model in {row["model"] for row in development}:
        assert {
            (row["config"], row["variant"]) for row in development if row["model"] == model
        } == {
            (config, variant)
            for config in ("A0", "A1", "A2", "A3", "A4", "A5", "B_prompt", "B_guard")
            for variant in ("attack", "legit_twin")
        }
    assert {"E2", "E3", "E4"} <= {row["experiment"] for row in rows}
    assert all(row["in_denominator"] is False for row in rows)
    assert all(row["corpus"] != "C2" for row in rows)
    from corridor.bench.consistency import fixed_subset
    from corridor.bench.interaction import COMMON_TRANSFERS

    assert all(row["case_id"] in COMMON_TRANSFERS for row in rows if row["experiment"] == "E2")
    assert all(row["case_id"] in fixed_subset() for row in rows if row["experiment"] == "E3")
    e4 = [group for group in comparison_groups(plan) if group[0]["experiment"] == "E4"]
    assert len(e4) == 2
    assert all({block["variant"] for block in group} == {"benign", "attack"} for group in e4)


def test_rehearsal_plan_never_loads_user_supplied_c2(tmp_path, monkeypatch):
    from argparse import Namespace

    from scripts import run_live_zh as runner

    root = tmp_path / "repository"
    (root / "docs").mkdir(parents=True)
    (root / "docs/PREREG-DOMESTIC.md").write_text("synthetic prereg", encoding="utf-8")
    monkeypatch.setattr(runner, "ROOT", root)
    read_paths = []

    def samples(path):
        read_paths.append(path)
        assert path == root / "tests/fixtures/blind-zh-sample"
        return [{"case_id": "dev"}]

    monkeypatch.setattr(runner, "load_cases", samples)
    runner.coordinate(
        Namespace(
            cases=root / "data/blind-zh",
            output=root / "evidence/rehearsal/test",
            rehearsal=True,
            episode_cap="2",
            domestic_api=None,
            execute=False,
        )
    )
    assert len(read_paths) == 2


def test_rehearsal_summary_does_not_count_refusal_as_successful_config_path(tmp_path):
    from corridor.bench.bank_cases import write_json
    from scripts.run_live_zh import rehearsal_summary

    block = {"block_id": "test"}
    write_json(
        tmp_path / "blocks/test.json",
        {
            "rows": [
                {
                    "experiment": "rehearsal",
                    "model": "synthetic",
                    "provider": "synthetic",
                    "config": "B_guard",
                    "status": "valid_completed",
                    "completed": False,
                    "metrics": {"tool_calls": 0},
                }
            ]
        },
    )
    result = rehearsal_summary([block], tmp_path)
    assert result["coverage"][0]["valid_observations"] == 1
    assert result["coverage"][0]["successful_paths"] == 0
    assert result["coverage"][0]["incomplete_reasons"]
