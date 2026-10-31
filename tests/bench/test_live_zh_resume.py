"""R5 admission and crash recovery use synthetic metadata and mock providers only."""

from fractions import Fraction

import pytest

from corridor.bench.live_zh import (
    BudgetJournal,
    SpendingJournal,
    build_pilot_plan,
    build_plan,
    comparison_groups,
    execute_block,
    verify_local_runtime,
)


def main_blocks():
    cases = [{"case_id": f"dev-{i}", "harm_class": "synthetic"} for i in range(2)]
    return [block for block in build_plan(cases) if block["priority"] == 2]


def test_fix_l3_admits_whole_case_comparisons_and_resume_reserves_once(tmp_path):
    from scripts.run_live_zh import reserve_comparison_block

    group = comparison_groups(main_blocks())[0]
    journal = BudgetJournal(tmp_path / "usd.jsonl", "USD", Fraction(10))
    spending = SpendingJournal(tmp_path / "spending.jsonl", "USD", Fraction(10))
    admission = reserve_comparison_block(group, journal, spending, Fraction(1, 50))
    assert admission["admitted"] is True
    assert admission["reserved"] == "63/50"
    before = journal.path.read_bytes()
    assert reserve_comparison_block(group, journal, spending, Fraction(1, 50)) == admission
    assert journal.path.read_bytes() == before
    assert admission["safety_factor"] == "3/2"
    assert admission["episode_count"] == 42
    assert admission["retry_reservation"] == "0"


def test_pilots_are_exactly_two_dev_samples_and_four_excluded_episodes():
    blocks = build_pilot_plan([{"case_id": "sample-b"}, {"case_id": "sample-a"}])
    assert len(blocks) == 2
    assert all(block["configs"] == ["A0", "A5"] for block in blocks)
    assert all(block["seed"] == 1 and block["in_denominator"] is False for block in blocks)
    assert all(block["corpus"] == "development_sample" for block in blocks)


def test_mock_complete_budget_cutoff_interrupt_and_resume(tmp_path):
    blocks = main_blocks()
    admission = {"admitted": ["A0", "A5"]}
    calls = []
    interrupt = True

    def run(row, attempt):
        nonlocal interrupt
        identity = (row["block_id"], row["config"], attempt)
        if interrupt and len(calls) == 1:
            interrupt = False
            raise KeyboardInterrupt
        calls.append(identity)
        return {"status": "valid_completed", "completed": True, "prohibited_effect": False}

    first = {**blocks[0], "configs": admission["admitted"]}
    progress = tmp_path / "progress" / (first["block_id"] + ".jsonl")
    with pytest.raises(KeyboardInterrupt):
        execute_block(first, run, lambda rows: True, progress=progress)
    result = execute_block(first, run, lambda rows: True, progress=progress)
    assert result["rows"][0]["status"] == "valid_completed"
    assert result["rows"][1]["status"] == "unknown"
    assert len(calls) == 1  # An uncertain in-flight call is never repeated automatically.
    for block in blocks[1:]:
        selected = [config for config in block["configs"] if config in admission["admitted"]]
        path = tmp_path / "progress" / (block["block_id"] + ".jsonl")
        execute_block({**block, "configs": selected}, run, lambda rows: True, progress=path)
    before = list(calls)
    for block in blocks:
        selected = [config for config in block["configs"] if config in admission["admitted"]]
        path = tmp_path / "progress" / (block["block_id"] + ".jsonl")
        execute_block({**block, "configs": selected}, run, lambda rows: True, progress=path)
    assert calls == before
    assert len(calls) == len(blocks) * 2 - 1
    assert len(calls) == len(set(calls))


@pytest.mark.parametrize("digest,vram", [("changed", 1024), ("fixed", 0), ("fixed", -1)])
def test_local_block_gpu_or_digest_failure_stops_without_service_management(digest, vram):
    lock = [{"tier": "medium", "model": "qwen3:8b", "registry_manifest_sha256": "fixed"}]
    status = {"models": [{"name": "qwen3:8b", "digest": digest, "size_vram": vram}]}
    with pytest.raises(ValueError):
        verify_local_runtime(lock, status, "qwen3-8b")
    status["models"][0].update(digest="fixed", size_vram=1024, context_length=32768)
    assert verify_local_runtime(lock, status, "qwen3-8b")["verified"]
    status["models"][0]["context_length"] = 8192
    with pytest.raises(ValueError, match="context length"):
        verify_local_runtime(lock, status, "qwen3-8b")


def test_domestic_api_is_additional_and_invalid_case_is_still_planned():
    blocks = build_plan(
        [{"case_id": "invalid-1", "status": "invalid_case"}], domestic_api="qwen-plus"
    )
    assert {row["model"] for row in blocks if row["priority"] == 3} == {"qwen3.8-27b", "qwen-plus"}
    assert any(row["case_id"] == "invalid-1" for row in blocks)
    assert all(row["provider"] != "domestic" for row in blocks if row["experiment"] in {"E2", "E3"})


def test_whole_plan_has_unique_reporting_identities():
    from corridor.bench.live_zh import planned_rows
    from scripts.final_report_zh import merge_planned_rows

    plan = build_plan([{"case_id": "synthetic-1", "harm_class": "synthetic"}])
    rows = [row for block in plan for row in planned_rows(block)]
    assert len(merge_planned_rows([], rows)) == len(rows)
    assert all(block["configs"] == ["B_guard"] for block in plan if block["priority"] == 6)


def test_completed_worker_result_recovers_after_missing_finish_record(tmp_path):
    block = {**main_blocks()[0], "configs": ["A0"]}
    progress = tmp_path / "progress.jsonl"
    calls = []

    def run(row, attempt):
        calls.append(row)
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        execute_block(block, run, lambda rows: True, progress=progress)
    result = execute_block(
        block,
        run,
        lambda rows: True,
        progress=progress,
        recover=lambda row, attempt: {"status": "valid_completed"},
    )
    assert len(calls) == 1
    assert result["rows"][0]["status"] == "valid_completed"


def test_strict_projection_ignores_api_measurements_but_keeps_business_effects():
    from corridor.bench.live_zh import replay_projection

    live = {"metrics": {"api_calls": 2, "cost_usd": "1/100", "tool_calls": 1}, "completed": True}
    strict = {"metrics": {"api_calls": 0, "cost_usd": "0", "tool_calls": 1}, "completed": True}
    assert replay_projection(live) == replay_projection(strict)
    strict["completed"] = False
    assert replay_projection(live) != replay_projection(strict)


def test_resume_network_failure_retries_only_remaining_same_identity(tmp_path):
    block = {**main_blocks()[0], "configs": ["A0"]}
    progress = tmp_path / "progress.jsonl"
    calls, replayed = [], []

    def run(row, attempt):
        calls.append((row["case_id"], row["seed"], row["config"], attempt))
        if attempt == 1:
            raise KeyboardInterrupt
        return {"status": "runtime_error", "error_class": "network_quota"}

    with pytest.raises(KeyboardInterrupt):
        execute_block(block, run, lambda rows: True, progress=progress)
    result = execute_block(
        block,
        run,
        lambda attempts: replayed.extend(attempts) or True,
        progress=progress,
        recover=lambda row, attempt: {
            "status": "runtime_error",
            "error_class": "network_quota",
        },
    )
    assert [row[3] for row in calls] == [0, 1, 2]
    assert len({row[:3] for row in calls}) == 1
    assert [row["attempt"] for row in replayed] == [0, 1, 2]
    assert result["block_passed"] is False
    execute_block(block, run, lambda rows: True, progress=progress)
    assert [row[3] for row in calls] == [0, 1, 2]


def test_actual_cost_ledger_releases_unused_hold_and_caps_total(tmp_path):
    ledger = SpendingJournal(tmp_path / "spent.jsonl", "USD", Fraction(3))
    assert ledger.begin("episode-0", Fraction(2)) == 2
    assert ledger.remaining() == 1
    ledger.settle("episode-0", Fraction(1, 4))
    assert ledger.remaining() == Fraction(11, 4)
    assert ledger.begin("episode-1", Fraction(2)) == 2
    assert ledger.begin("episode-2", Fraction(2)) == Fraction(3, 4)
    assert ledger.begin("episode-3", Fraction(2)) == 0
    assert ledger.remaining() == 0


def test_actual_cost_ledger_preserves_crash_hold_and_idempotent_resume(tmp_path):
    path = tmp_path / "spent.jsonl"
    ledger = SpendingJournal(path, "USD", Fraction(120))
    ledger.begin("same-attempt", Fraction(2))
    before = path.read_bytes()
    resumed = SpendingJournal(path, "USD", Fraction(120))
    assert resumed.begin("same-attempt", Fraction(2)) == 2
    assert path.read_bytes() == before
    assert resumed.remaining() == 118
    resumed.settle("same-attempt", Fraction(1, 10))
    before = path.read_bytes()
    resumed.settle("same-attempt", Fraction(1, 10))
    assert path.read_bytes() == before
    assert resumed.remaining() == Fraction(1199, 10)
    with pytest.raises(ValueError, match="identity"):
        resumed.settle("same-attempt", Fraction(1, 5))
    with pytest.raises(ValueError, match="currency"):
        SpendingJournal(path, "CNY", Fraction(120)).remaining()


def test_actual_cost_ledger_rejects_unreserved_or_excess_cost(tmp_path):
    ledger = SpendingJournal(tmp_path / "spent.jsonl", "USD", Fraction(120))
    with pytest.raises(ValueError, match="Unknown"):
        ledger.settle("absent", Fraction(1))
    ledger.begin("reserved", Fraction(2))
    for cost in (Fraction(-1), Fraction(3)):
        with pytest.raises(ValueError, match="outside"):
            ledger.settle("reserved", cost)


def test_provider_cost_overrun_is_retained_and_freezes_further_paid_allowances(tmp_path):
    path = tmp_path / "spent.jsonl"
    ledger = SpendingJournal(path, "USD", Fraction(120))
    assert ledger.begin("unexpected-charge", Fraction(2)) == 2
    with pytest.raises(ValueError, match="outside"):
        ledger.settle("unexpected-charge", Fraction(3))
    from corridor.schemas.base import loads_exact

    overrun = loads_exact(path.read_bytes().splitlines()[-1])
    assert overrun["state"] == "overrun" and overrun["amount"] == "3"
    assert ledger.remaining() == 0
    assert ledger.begin("next-paid", Fraction(2)) == 0
    resumed = SpendingJournal(path, "USD", Fraction(120))
    assert resumed.begin("after-resume", Fraction(2)) == 0
    with pytest.raises(ValueError, match="outside"):
        resumed.settle("unexpected-charge", Fraction(3))
