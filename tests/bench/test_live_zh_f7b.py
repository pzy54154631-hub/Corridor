"""Freeze-7b continuation and domestic rehearsal invariants, without live calls."""

from pathlib import Path

import pytest

from corridor.bench.bank_cases import write_json
from corridor.bench.live_zh import (
    build_plan,
    build_rehearsal_plan,
    comparison_groups,
    planned_rows,
    report_row_sets,
    select_row_set,
)
from corridor.crypto.jcs import H
from corridor.schemas.base import loads_exact


def domestic_probe():
    return {
        "providers": {
            provider: {"gate_passed": True, "models": [{"model": model}]}
            for provider, model in (("deepseek", "deepseek-flash"), ("zhipu", "glm-5.1"))
        }
    }


def frozen_source(path, blocks):
    write_json(
        path / "plan.json", {"blocks": blocks, "rows": [r for b in blocks for r in planned_rows(b)]}
    )
    for block in blocks:
        write_json(path / "blocks" / (block["block_id"] + ".json"), {"rows": planned_rows(block)})


def test_deepseek_probe_model_and_legacy_alias_share_plan_identity():
    probe = domestic_probe()
    actual = build_plan([], domestic_api="deepseek-flash", probe=probe)
    assert actual == build_plan([], domestic_api="deepseek-chat", probe=probe)
    assert actual == build_plan([], domestic_api="deepseek", probe=probe)
    rows = build_plan([{"case_id": "sample"}], domestic_api="deepseek-chat", probe=probe)
    assert {b["model"] for b in rows if b["provider"] == "deepseek"} == {"deepseek-flash"}


def test_domestic_plan_identity_binds_request_adaptation_only_for_domestic_rows():
    probe = domestic_probe()
    model = probe["providers"]["deepseek"]["models"][0]
    model["deployment_profile"] = {"thinking": "disabled", "tool_choice_mode": "forced_function"}
    cases = [{"case_id": "sample"}]
    before = build_plan(cases, domestic_api="deepseek", probe=probe)
    model["deployment_profile"]["tool_choice_mode"] = "auto"
    after = build_plan(cases, domestic_api="deepseek", probe=probe)
    assert [b for b in before if b["provider"] != "deepseek"] == [
        b for b in after if b["provider"] != "deepseek"
    ]
    assert {b["block_id"] for b in before if b["provider"] == "deepseek"}.isdisjoint(
        b["block_id"] for b in after if b["provider"] == "deepseek"
    )


def test_domestic_rehearsal_covers_only_development_twins_and_three_configs():
    blocks = build_rehearsal_plan(
        [{"case_id": "sample"}], domestic_api=["deepseek", "zhipu"], probe=domestic_probe()
    )
    assert {b["provider"] for b in blocks} == {"deepseek", "zhipu"}
    assert len(blocks) == 4
    assert {b["variant"] for b in blocks} == {"attack", "legit_twin"}
    assert all(b["configs"] == ["A0", "A5", "B_prompt"] for b in blocks)
    assert all(b["corpus"] == "development_sample" and not b["in_denominator"] for b in blocks)


def test_continuation_includes_all_e4_primary_blocks_after_dollar_cutoff(tmp_path):
    blocks = [b for b in build_plan([{"case_id": "sample"}]) if b["provider"] == "openai"]
    blocks = [b for b in blocks if b["priority"] == 2 or b["experiment"] == "E4"]
    frozen_source(tmp_path, blocks)
    groups = comparison_groups(blocks)
    first = groups[0]
    group_id = H([b["block_id"] for b in first])[:24]
    write_json(
        tmp_path / "budget-cutoffs/USD.json", {"currency": "USD", "comparison_block_id": group_id}
    )
    for group in groups:
        for block in group:
            is_e4_main = block["experiment"] == "E4" and block["variant"] != "pilot"
            write_json(
                tmp_path / "blocks" / (block["block_id"] + ".json"),
                {
                    "rows": planned_rows(block),
                    "reason": "E4 paid pilot or remaining-budget gate"
                    if is_e4_main
                    else "Complete case comparison budget admission",
                    **(
                        {}
                        if is_e4_main
                        else {"comparison_block_id": H([b["block_id"] for b in group])[:24]}
                    ),
                },
            )
    selected, _ = select_row_set([], row_set="cut-continuation", continuation_of=tmp_path)
    assert selected == blocks
    assert len([b for b in selected if b["experiment"] == "E4" and b["variant"] != "pilot"]) == 507


@pytest.mark.parametrize(
    "marker",
    [{"termination": "budget_truncated"}, {"early_stop": {"reason": "shared_budget_exhausted"}}],
)
def test_partially_spent_comparison_is_replaced_whole(tmp_path, marker):
    blocks = [b for b in build_plan([{"case_id": "sample"}]) if b["priority"] == 2]
    frozen_source(tmp_path, blocks)
    block = blocks[2]
    rows = [{**r, "status": "valid_completed"} for r in planned_rows(block)]
    rows[-1].update(marker)
    write_json(tmp_path / "blocks" / (block["block_id"] + ".json"), {"rows": rows})
    assert select_row_set([], row_set="cut-continuation", continuation_of=tmp_path)[0] == blocks


def test_e4_pilot_internal_exhaustion_selects_main_blocks_without_cutoff_file(tmp_path):
    blocks = [b for b in build_plan([]) if b["experiment"] == "E4" and b["provider"] == "openai"]
    frozen_source(tmp_path, blocks)
    for block in blocks:
        report = {"rows": planned_rows(block)}
        if block["variant"] == "pilot":
            report["rows"][-1]["termination"] = "budget_truncated"
        else:
            report["reason"] = "E4 paid pilot or remaining-budget gate"
        write_json(tmp_path / "blocks" / (block["block_id"] + ".json"), report)
    assert not (tmp_path / "budget-cutoffs/USD.json").exists()
    assert select_row_set([], row_set="cut-continuation", continuation_of=tmp_path)[0] == blocks


def test_source_completion_rejects_missing_blocks_and_owned_lock(tmp_path):
    from scripts.run_live_zh import coordinator_lock, require_finished_source

    blocks = [b for b in build_plan([{"case_id": "sample"}]) if b["priority"] == 2]
    frozen_source(tmp_path, blocks)
    require_finished_source(tmp_path)
    with coordinator_lock(tmp_path / ".coordinator.lock"):
        with pytest.raises(ValueError, match="owns|active"):
            require_finished_source(tmp_path)
    missing = blocks[-1]["block_id"]
    (tmp_path / "blocks" / (missing + ".json")).unlink()
    with pytest.raises(ValueError, match=missing):
        require_finished_source(tmp_path)


def test_proof_replays_all_providers_and_rejects_changed_source(tmp_path, monkeypatch):
    from scripts import run_live_zh as runner

    source, output = tmp_path / "source", tmp_path / "proof.json"
    blocks = []
    for index, provider in enumerate(("openai", "local", "deepseek", "zhipu")):
        payload = {
            "priority": 1,
            "provider": provider,
            "model": "synthetic",
            "experiment": "E2",
            "case_id": str(index),
            "configs": ["A0"],
            "seed": 1,
            "variant": "benign",
        }
        blocks.append({**payload, "block_id": H(payload)[:24]})
    frozen_source(source, blocks)
    for block in blocks:
        folder = source / "episodes" / block["block_id"] / "A0" / "0"
        result = {**planned_rows(block)[0], "status": "valid_completed"}
        write_json(folder / "result.json", result)
        write_json(
            folder / "spec.json",
            {"row": planned_rows(block)[0], "replay_dir": "obsolete", "result_path": "obsolete"},
        )
    observed = []

    def replay(spec, destination):
        observed.append(spec["row"]["provider"])
        write_json(destination, loads_exact(Path(spec["result_path"]).read_bytes()))
        return True

    monkeypatch.setattr(runner, "strict_source_worker", replay)
    proof = runner.prove_source_replay(source, output)
    assert proof["replayed"] == proof["consistent"] == 4
    assert proof["inconsistent"] == 0
    assert set(observed) == {"openai", "local", "deepseek", "zhipu"}
    runner.require_source_replay_proof(source, output)
    write_json(source / "blocks" / (blocks[0]["block_id"] + ".json"), {"rows": []})
    with pytest.raises(ValueError, match="hash|coverage"):
        runner.require_source_replay_proof(source, output)


def test_proof_rejects_observed_block_rows_without_episode_artifacts(tmp_path):
    from scripts import run_live_zh as runner

    source, output = tmp_path / "source", tmp_path / "proof.json"
    blocks = [b for b in build_plan([{"case_id": "sample"}]) if b["priority"] == 2]
    frozen_source(source, blocks)
    block = blocks[0]
    write_json(
        source / "blocks" / (block["block_id"] + ".json"),
        {
            "rows": [{**r, "status": "valid_completed"} for r in planned_rows(block)],
        },
    )
    proof = runner.prove_source_replay(source, output)
    assert proof["replayed"] == 0
    assert proof["passed"] is False
    assert len(proof["missing_source_attempts"]) == len(block["configs"])
    with pytest.raises(ValueError, match="coverage"):
        runner.require_source_replay_proof(source, output)


def test_proof_refuses_preexisting_unit_directory_without_changing_evidence(tmp_path):
    from scripts import run_live_zh as runner

    source, output = tmp_path / "source", tmp_path / "proof.json"
    blocks = [b for b in build_plan([{"case_id": "sample"}]) if b["priority"] == 2]
    frozen_source(source, blocks)
    stale = tmp_path / "proof-units" / "retained.txt"
    stale.parent.mkdir()
    stale.write_text("retained diagnostic evidence", encoding="utf-8")
    with pytest.raises(ValueError, match="fresh"):
        runner.prove_source_replay(source, output)
    assert stale.read_text(encoding="utf-8") == "retained diagnostic evidence"
    assert not output.exists()


def test_reporting_separates_pilots_auxiliary_excluded_and_exploratory():
    rows = [
        {"id": "main", "experiment": "E1"},
        {"id": "completion", "experiment": "E2"},
        {"id": "pilot", "experiment": "pilot", "in_denominator": False},
        {"id": "aux", "experiment": "E4"},
        {"id": "excluded", "experiment": "E1", "in_denominator": False},
        {"id": "explore", "experiment": "E1", "exploratory": True, "in_denominator": False},
    ]
    sections = report_row_sets(rows)
    assert [r["id"] for r in sections["preregistered"]["rows"]] == ["main", "completion"]
    assert sections["pilots"]["rows"] == [rows[2]]
    assert sections["auxiliary"]["rows"] == [rows[3]]
    assert sections["excluded"]["rows"] == [rows[4]]
    assert sections["exploratory"]["rows"] == [rows[5]]


def test_stale_domestic_probe_cannot_start_worker():
    from scripts.run_live_zh import _profile

    probe = domestic_probe()
    with pytest.raises(ValueError, match="formal-shape"):
        _profile({"provider": "deepseek", "model": "deepseek-flash", "seed": 1}, [], probe)


def test_domestic_worker_environment_is_isolated_and_other_providers_unchanged(monkeypatch):
    from scripts.run_live_zh import domestic_worker_options

    monkeypatch.setenv("OPENAI_CUSTOM_HEADERS", "fake-contamination")
    monkeypatch.setenv("LLM_MODEL", "fake-other-model")
    monkeypatch.setenv("ZHIPU_API_KEY", "fake-other-key")
    for provider in ("deepseek", "zhipu"):
        environment = domestic_worker_options(provider)["env"]
        assert "OPENAI_CUSTOM_HEADERS" not in environment
        assert "LLM_MODEL" not in environment
        assert "ZHIPU_API_KEY" not in environment
    assert domestic_worker_options("openai") == domestic_worker_options("local") == {}


def test_rehearsal_diagnostics_include_failure_rate_output_length_and_replay(tmp_path):
    from scripts.run_live_zh import rehearsal_summary

    blocks = build_rehearsal_plan(
        [{"case_id": "sample"}], domestic_api="deepseek", probe=domestic_probe()
    )
    for block in blocks:
        rows = [
            {**r, "status": "runtime_error", "error_class": "model_output_format"}
            for r in planned_rows(block)
        ]
        attempts = [
            {
                **r,
                "usage_measurements": {
                    "model_requests": 2,
                    "output_tokens": 100,
                    "finish_reason_length": 1,
                },
            }
            for r in rows
        ]
        write_json(
            tmp_path / "blocks" / (block["block_id"] + ".json"),
            {
                "rows": rows,
                "attempts": attempts,
                "replay_passed": True,
            },
        )
    summary = rehearsal_summary(blocks, tmp_path)
    assert summary["apparatus_defect"] == 0
    assert summary["finish_reason_length"] == 6
    assert summary["strict_replay_passed"] is True
    assert summary["providers"][0]["format_failure_rate"] == "1"
    assert summary["providers"][0]["mean_output_tokens_per_request"] == "50"
