"""Freeze-7 scheduling uses synthetic metadata and never opens author cases."""

import hashlib
from argparse import Namespace
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace

import pytest

from corridor.bench.bank_cases import write_json
from corridor.bench.live_zh import BUDGET_CAPS, build_plan, comparison_groups, planned_rows
from corridor.crypto.jcs import H
from corridor.schemas.base import loads_exact


def probe():
    return {
        "providers": {
            "zhipu": {
                "gate_passed": True,
                "models": [{"model": "glm-synthetic", "strict_replay": True}],
            }
        }
    }


def test_budget_caps_are_emergency_brakes_without_changing_local_cost():
    assert BUDGET_CAPS == {"USD": Fraction(5000), "CNY": Fraction(30000), "LOCAL": Fraction(0)}


def test_repeated_domestic_models_keep_providers_pairing_and_seed_rotation():
    cases = [{"case_id": "sample-b"}, {"case_id": "sample-a"}]
    before = build_plan(cases)
    after = build_plan(cases, domestic_api=["deepseek-chat", "zhipu", "qwen-plus"], probe=probe())
    assert [row for row in after if row["provider"] in {"openai", "local"}] == before
    expected = {"deepseek-chat": "deepseek", "glm-synthetic": "zhipu", "qwen-plus": "qwen-plus"}
    for model, provider in expected.items():
        rows = [row for row in after if row["model"] == model]
        assert len(rows) == 12
        assert {row["provider"] for row in rows} == {provider}
        assert {row["seed"] for row in rows} == {1, 2, 3}
        assert {row["variant"] for row in rows} == {"attack", "legit_twin"}
        assert all(row["experiment"] == "E1" and row["corpus"] == "C2" for row in rows)
        assert all(sorted(row["configs"]) == ["A0", "A5", "B_prompt"] for row in rows)
        assert rows[0]["configs"] == ["A0", "A5", "B_prompt"]
        assert rows[6]["configs"] == ["A5", "B_prompt", "A0"]
        assert all(row.get("exploratory", False) is (provider == "zhipu") for row in rows)
        if provider == "zhipu":
            assert all(row["main_hypothesis"] is False for row in rows)
            assert all(row["in_denominator"] is False for row in rows)
    assert len({row["block_id"] for row in after}) == len(after)


def test_zhipu_exact_probe_model_and_repeated_selection_are_deterministic():
    cases = [{"case_id": "sample"}]
    by_provider = build_plan(cases, domestic_api=["zhipu", "zhipu"], probe=probe())
    by_model = build_plan(cases, domestic_api=["glm-synthetic"], probe=probe())
    assert by_provider == by_model
    assert len([row for row in by_provider if row["provider"] == "zhipu"]) == 6
    with pytest.raises(ValueError, match="probed"):
        build_plan([], domestic_api=["glm-unknown"], probe=probe())
    with pytest.raises(ValueError, match="probed"):
        build_plan([], domestic_api=["zhipu"])
    failed = probe()
    failed["providers"]["zhipu"]["gate_passed"] = False
    with pytest.raises(ValueError, match="probed"):
        build_plan([], domestic_api=["glm-synthetic"], probe=failed)


def test_row_sets_only_select_requested_providers_and_partition_exploratory():
    from corridor.bench.live_zh import report_row_sets, select_row_set

    blocks = build_plan(
        [{"case_id": "sample"}], domestic_api=["deepseek-chat", "zhipu"], probe=probe()
    )
    selected, identity = select_row_set(blocks, row_set="domestic-api")
    assert identity == {"row_set": "domestic-api", "source_plan_sha256": None}
    assert len(selected) == 12
    assert {row["provider"] for row in selected} == {"deepseek", "zhipu"}
    rows = [row for block in selected for row in planned_rows(block)]
    sections = report_row_sets(rows)
    assert sections["exploratory"]["included_in_main_hypotheses"] is False
    assert {row["provider"] for row in sections["exploratory"]["rows"]} == {"zhipu"}
    assert {row["provider"] for row in sections["preregistered"]["rows"]} == {"deepseek"}
    assert report_row_sets([{"provider": "zhipu"}])["preregistered"]["rows"] == []
    assert select_row_set(blocks)[0] == blocks
    with pytest.raises(ValueError, match="row set"):
        select_row_set(blocks, row_set="unknown")


def test_legacy_report_excludes_zhipu_from_primary_summaries_and_hypotheses():
    from scripts.final_report_zh import build_report

    rows = [
        row
        for block in build_plan([{"case_id": "sample"}], domestic_api="zhipu", probe=probe())
        if block["provider"] == "zhipu"
        for row in planned_rows(block)
    ]
    _text, report = build_report(rows, dryrun=False)
    assert report["summaries"] == report["paired"] == []
    assert report["holm"] == {}
    assert report["pilots"] == []
    assert len(report["exploratory_deployments"]) == 6
    assert sum(len(item["rows"]) for item in report["exploratory_deployments"]) == len(rows)


def source_plan(tmp_path):
    blocks = [
        row
        for row in build_plan([{"case_id": "sample-a"}, {"case_id": "sample-b"}])
        if row["priority"] == 2
    ]
    manifest = {
        "schema_version": "zh-live-plan-v4",
        "case_hash": H([{"case_id": "sample-a"}, {"case_id": "sample-b"}]),
        "episode_cap": "2",
        "blocks": blocks,
        "rows": [row for block in blocks for row in planned_rows(block)],
    }
    write_json(tmp_path / "plan.json", manifest)
    return manifest


def cutoff(tmp_path, blocks):
    group_id = H([block["block_id"] for block in blocks])[:24]
    write_json(
        tmp_path / "budget-cutoffs/USD.json",
        {"currency": "USD", "comparison_block_id": group_id},
    )
    for block in blocks:
        write_json(
            tmp_path / "blocks" / (block["block_id"] + ".json"),
            {
                "rows": planned_rows(block),
                "reason": "Complete case comparison budget admission",
                "comparison_block_id": group_id,
            },
        )


def test_continuation_keeps_original_blocks_and_exact_source_hash(tmp_path):
    from corridor.bench.live_zh import select_row_set

    manifest = source_plan(tmp_path)
    selected = comparison_groups(manifest["blocks"])[1]
    cutoff(tmp_path, selected)
    observed, identity = select_row_set([], row_set="cut-continuation", continuation_of=tmp_path)
    assert observed == selected
    assert identity == {
        "row_set": "cut-continuation",
        "source_plan_sha256": hashlib.sha256((tmp_path / "plan.json").read_bytes()).hexdigest(),
    }
    assert all(row in manifest["blocks"] for row in observed)


def test_continuation_does_not_infer_unrecorded_or_completed_trailing_blocks(tmp_path):
    from corridor.bench.live_zh import select_row_set

    manifest = source_plan(tmp_path)
    groups = comparison_groups(manifest["blocks"])
    cutoff(tmp_path, groups[0])
    write_json(
        tmp_path / "blocks" / (groups[1][0]["block_id"] + ".json"),
        {"rows": [{**row, "status": "valid_completed"} for row in planned_rows(groups[1][0])]},
    )
    assert select_row_set([], row_set="cut-continuation", continuation_of=tmp_path)[0] == groups[0]


def test_legacy_marker_selects_all_recorded_trailing_comparisons_in_source_order(tmp_path):
    from corridor.bench.live_zh import select_row_set

    manifest = source_plan(tmp_path)
    groups = comparison_groups(manifest["blocks"])
    cutoff(tmp_path, groups[1])
    cutoff(tmp_path, groups[0])
    assert (
        select_row_set([], row_set="cut-continuation", continuation_of=tmp_path)[0]
        == manifest["blocks"]
    )


def test_continuation_rejects_cutoff_marker_for_the_wrong_currency(tmp_path):
    from corridor.bench.live_zh import select_row_set

    manifest = source_plan(tmp_path)
    group = comparison_groups(manifest["blocks"])[0]
    write_json(
        tmp_path / "budget-cutoffs/CNY.json",
        {"currency": "CNY", "comparison_block_id": H([row["block_id"] for row in group])[:24]},
    )
    with pytest.raises(ValueError, match="currency"):
        select_row_set([], row_set="cut-continuation", continuation_of=tmp_path)


def test_no_cutoff_has_no_selected_blocks_and_never_opens_cases(tmp_path):
    from corridor.bench.live_zh import select_row_set

    source_plan(tmp_path)
    assert select_row_set([], row_set="cut-continuation", continuation_of=tmp_path)[0] == []
    with pytest.raises(ValueError, match="continuation"):
        select_row_set([], row_set="cut-continuation")
    with pytest.raises(ValueError, match="continuation"):
        select_row_set([], row_set="all", continuation_of=tmp_path)


@pytest.mark.parametrize("mutation", ["block", "row", "duplicate", "unknown_cutoff", "report"])
def test_continuation_rejects_changed_or_inconsistent_source(tmp_path, mutation):
    from corridor.bench.live_zh import select_row_set

    manifest = source_plan(tmp_path)
    group = comparison_groups(manifest["blocks"])[1]
    cutoff(tmp_path, group)
    if mutation == "block":
        manifest["blocks"][0]["seed"] = 4
    elif mutation == "row":
        manifest["rows"][0]["config"] = "A1"
    elif mutation == "duplicate":
        manifest["blocks"].append(manifest["blocks"][0])
    elif mutation == "unknown_cutoff":
        write_json(
            tmp_path / "budget-cutoffs/USD.json",
            {"currency": "USD", "comparison_block_id": "unknown"},
        )
    else:
        write_json(
            tmp_path / "blocks" / (group[0]["block_id"] + ".json"),
            {"rows": [], "reason": "Complete case comparison budget admission"},
        )
    write_json(tmp_path / "plan.json", manifest)
    with pytest.raises(ValueError):
        select_row_set([], row_set="cut-continuation", continuation_of=tmp_path)


def test_continuation_rejects_incomplete_cut_comparison_group(tmp_path):
    from corridor.bench.live_zh import select_row_set

    manifest = source_plan(tmp_path)
    group = comparison_groups(manifest["blocks"])[0]
    cutoff(tmp_path, group)
    (tmp_path / "blocks" / (group[-1]["block_id"] + ".json")).unlink()
    with pytest.raises(ValueError, match="incomplete"):
        select_row_set([], row_set="cut-continuation", continuation_of=tmp_path)


def continuation_coordinator(tmp_path, monkeypatch):
    from scripts import run_live_zh as runner

    source = tmp_path / "source"
    manifest = source_plan(source)
    selected = comparison_groups(manifest["blocks"])[1]
    cutoff(source, selected)
    for block in manifest["blocks"]:
        path = source / "blocks" / (block["block_id"] + ".json")
        if not path.exists():
            write_json(path, {"rows": planned_rows(block)})
    root = tmp_path / "repo"
    (root / "docs").mkdir(parents=True)
    (root / "docs/PREREG-DOMESTIC.md").write_text("Synthetic prereg", encoding="utf-8")
    monkeypatch.setattr(runner, "ROOT", root)
    monkeypatch.setattr(
        runner, "load_cases", lambda *a: [{"case_id": "sample-a"}, {"case_id": "sample-b"}]
    )
    monkeypatch.setattr(runner, "build_plan", lambda *a, **kw: pytest.fail("rebuilt source plan"))
    monkeypatch.setattr(runner, "schedule_blocks", lambda *a: pytest.fail("reordered source plan"))
    # Proof validation has independent content/hash/coverage tests; this fixture
    # isolates scheduling and uses intentionally synthetic block-only records.
    monkeypatch.setattr(runner, "require_source_replay_proof", lambda *a: {})
    args = Namespace(
        cases=root / "synthetic-cases",
        output=tmp_path / "continued",
        row_set="cut-continuation",
        continuation_of=source,
        domestic_api=[],
        episode_cap="2",
        execute=False,
    )
    return runner, args, selected


def test_continuation_coordinator_retains_exact_order_without_new_pilots(tmp_path, monkeypatch):
    runner, args, selected = continuation_coordinator(tmp_path, monkeypatch)
    runner.coordinate(args)
    observed = loads_exact((args.output / "plan.json").read_bytes())
    assert observed["blocks"] == selected
    assert observed["rows"] == [row for block in selected for row in planned_rows(block)]
    assert observed["row_set"] == "cut-continuation"
    assert (
        observed["source_plan_sha256"]
        == hashlib.sha256((args.continuation_of / "plan.json").read_bytes()).hexdigest()
    )
    assert not any(block["experiment"] == "pilot" for block in observed["blocks"])


@pytest.mark.parametrize("changed", ["episode_cap", "nested_output", "domestic_api", "stages"])
def test_continuation_coordinator_rejects_configuration_changes(tmp_path, monkeypatch, changed):
    runner, args, _selected = continuation_coordinator(tmp_path, monkeypatch)
    if changed == "episode_cap":
        args.episode_cap = "1"
    elif changed == "nested_output":
        args.output = args.continuation_of / "nested"
    elif changed == "domestic_api":
        args.domestic_api = ["deepseek-chat"]
    else:
        args.stages = "E1"
    with pytest.raises(ValueError):
        runner.coordinate(args)


def test_retained_continuation_cannot_add_domestic_blocks(tmp_path):
    from scripts.run_live_zh import retain_plan

    path = tmp_path / "plan.json"
    original = {
        "row_set": "cut-continuation",
        "source_plan_sha256": "a" * 64,
        "blocks": [],
        "rows": [],
    }
    retain_plan(path, original)
    addition = [
        row
        for row in build_plan([{"case_id": "synthetic"}], domestic_api="deepseek-chat")
        if row["provider"] == "deepseek"
    ]
    with pytest.raises(ValueError, match="frozen run identity"):
        retain_plan(
            path,
            {
                **original,
                "blocks": addition,
                "rows": [r for b in addition for r in planned_rows(b)],
            },
        )


def mock_execution(runner, monkeypatch):
    """Run the real coordinator with local synthetic files and an inert subprocess."""
    write_json(runner.ROOT / "local/models-lock.json", [])
    monkeypatch.setattr(runner, "LOCAL_ROOT", runner.ROOT / "local")
    monkeypatch.setattr(runner, "run_formal_gate", lambda **kw: {"ready": True})
    monkeypatch.setattr(runner, "workspace_clean", lambda *a: None)
    monkeypatch.setattr(runner, "load_provider_credentials", lambda *a, **kw: object())
    monkeypatch.setattr(runner, "require_global_probe_gate", lambda *a: None)
    monkeypatch.setattr(runner, "load_run_probe", lambda *a: {"providers": {}})
    monkeypatch.setattr(runner, "verify_local_lock", lambda *a: {"verified": True})
    monkeypatch.setattr(runner, "local_status", lambda: {})
    monkeypatch.setattr(runner, "_profile", lambda row, *a: {"model": row["model"]})
    monkeypatch.setattr(runner.subprocess, "check_output", lambda *a, **kw: "domestic-freeze-7")
    observed = {"record": [], "strict": []}

    def process(command, **kwargs):
        if "--worker" not in command:
            return SimpleNamespace(returncode=0)
        spec = loads_exact(Path(command[command.index("--worker") + 1]).read_bytes())
        output = Path(command[command.index("--output") + 1])
        strict = "--strict" in command
        observed["strict" if strict else "record"].append(spec)
        result = (
            loads_exact(Path(spec["result_path"]).read_bytes())
            if strict
            else {
                **spec["row"],
                "status": "valid_completed",
                "completed": True,
                "prohibited_effect": False,
                "usage_measurements": {
                    "cost": "1/100",
                    "currency": spec["currency"],
                    "total_tokens": 100,
                },
            }
        )
        write_json(output, result)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(runner.subprocess, "run", process)
    return observed


def test_domestic_only_coordinator_records_cny_ceiling_and_never_adds_openai_pilots(
    tmp_path, monkeypatch
):
    from corridor.bench.live_zh import schedule_blocks
    from scripts import run_live_zh as runner

    root = tmp_path / "repo"
    (root / "docs").mkdir(parents=True)
    (root / "docs/PREREG-DOMESTIC.md").write_text("Synthetic prereg", encoding="utf-8")
    cases = [{"case_id": "sample-a"}, {"case_id": "sample-b"}]
    complete = build_plan(cases, domestic_api=["deepseek-chat", "zhipu"], probe=probe())
    blocks = [row for row in complete if row["priority"] in {2, 3}]
    monkeypatch.setattr(runner, "ROOT", root)
    monkeypatch.setattr(runner, "load_cases", lambda *a: cases)
    monkeypatch.setattr(runner, "build_plan", lambda *a, **kw: blocks)
    observed = mock_execution(runner, monkeypatch)
    args = Namespace(
        cases=root / "synthetic-cases",
        output=tmp_path / "domestic",
        row_set="domestic-api",
        domestic_api=["deepseek-chat", "zhipu"],
        provider_env=["deepseek=synthetic-deepseek.env", "zhipu=synthetic-zhipu.env"],
        episode_cap="2",
        execute=True,
    )
    runner.coordinate(args)
    expected = [row for row in schedule_blocks(blocks) if row["provider"] in {"deepseek", "zhipu"}]
    assert loads_exact((args.output / "plan.json").read_bytes())["blocks"] == expected
    assert len(observed["record"]) == len(observed["strict"]) == len(expected) * 3
    assert {spec["row"]["provider"] for spec in observed["record"]} == {"deepseek", "zhipu"}
    assert all(
        spec["currency"] == "CNY" and spec["episode_cap"] == "15" for spec in observed["record"]
    )
    assert all(spec["row"]["experiment"] == "E1" for spec in observed["record"])
    assert not (args.output / "spending-USD.jsonl").exists()
    sections = loads_exact((args.output / "report-row-sets.json").read_bytes())
    assert {row["provider"] for row in sections["exploratory"]["rows"]} == {"zhipu"}
    assert sections["exploratory"]["included_in_main_hypotheses"] is False


def test_domestic_rerun_uses_cny_episode_ceiling_without_token_pilot(tmp_path, monkeypatch):
    from corridor.bench.rerun_zh import create_manifest
    from corridor.crypto.jcs import H
    from scripts import run_live_zh as runner

    root = tmp_path / "repo"
    (root / "docs").mkdir(parents=True)
    (root / "docs/PREREG-DOMESTIC.md").write_text("Synthetic prereg", encoding="utf-8")
    cases = [{"case_id": "sample-a"}]
    blocks = [
        b
        for b in build_plan(cases, domestic_api=["deepseek-chat"], probe=probe())
        if b["provider"] == "deepseek"
    ]
    source = tmp_path / "source" / "plan.json"
    write_json(
        source,
        {
            "blocks": blocks,
            "rows": [r for b in blocks for r in planned_rows(b)],
            "episode_cap": "2",
            "case_hash": H(cases),
        },
    )
    for block in blocks:
        write_json(
            source.parent / "blocks" / (block["block_id"] + ".json"),
            {"block_id": block["block_id"], "rows": planned_rows(block)},
        )
    manifest = tmp_path / "selection.json"
    write_json(manifest, create_manifest(source, "LIVE-7", "S2", [b["block_id"] for b in blocks]))
    monkeypatch.setattr(runner, "ROOT", root)
    monkeypatch.setattr(runner, "load_cases", lambda *a: cases)
    observed = mock_execution(runner, monkeypatch)
    from corridor.bench import rerun_proof

    monkeypatch.setattr(
        rerun_proof,
        "verify_bundle",
        lambda *a, **kw: {"bundle_path": "synthetic-proof.json", "bundle_sha256": "a" * 64},
    )
    args = Namespace(
        cases=root / "synthetic",
        output=tmp_path / "rerun",
        row_set="rerun",
        rerun_manifest=manifest,
        rerun_proof=tmp_path / "synthetic-proof.json",
        provider_env=["deepseek=synthetic-deepseek.env"],
        domestic_api=[],
        episode_cap="2",
        execute=True,
    )
    runner.coordinate(args)
    assert len(observed["record"]) == sum(len(b["configs"]) for b in blocks)
    assert all(
        spec["currency"] == "CNY" and spec["episode_cap"] == "15" for spec in observed["record"]
    )
    assert (args.output / "spending-CNY.jsonl").is_file()


def test_continuation_execution_uses_source_rows_and_existing_pilot_estimate(tmp_path, monkeypatch):
    from corridor.bench.live_zh import build_pilot_plan

    runner, args, selected = continuation_coordinator(tmp_path, monkeypatch)
    source = args.continuation_of
    manifest = loads_exact((source / "plan.json").read_bytes())
    pilots = build_pilot_plan([{"case_id": "sample-a"}, {"case_id": "sample-b"}])
    manifest["blocks"] = pilots + manifest["blocks"]
    manifest["rows"] = [row for block in manifest["blocks"] for row in planned_rows(block)]
    write_json(source / "plan.json", manifest)
    for block in pilots:
        write_json(
            source / "blocks" / (block["block_id"] + ".json"),
            {
                "replay_passed": True,
                "block_passed": True,
                "rows": [
                    {
                        **row,
                        "status": "valid_completed",
                        "usage_measurements": {"cost": "1/100", "total_tokens": 100},
                    }
                    for row in planned_rows(block)
                ],
            },
        )
    observed = mock_execution(runner, monkeypatch)
    args.execute = True
    runner.coordinate(args)
    expected = [row for block in selected for row in planned_rows(block)]
    assert [spec["row"] for spec in observed["record"]] == expected
    assert len(observed["strict"]) == len(expected)
    assert all(spec["episode_cap"] == "2" for spec in observed["record"])
    assert all(spec["row"]["experiment"] == "E1" for spec in observed["record"])


def test_selected_e4_pilots_do_not_preload_truncated_source_rows(tmp_path, monkeypatch):
    from corridor.bench.live_zh import build_pilot_plan

    runner, args, _ = continuation_coordinator(tmp_path, monkeypatch)
    development = build_pilot_plan([{"case_id": "sample-a"}, {"case_id": "sample-b"}])
    e4 = [b for b in build_plan([]) if b["experiment"] == "E4" and b["provider"] == "openai"]
    pilots = [b for b in e4 if b["variant"] == "pilot"]
    main = [b for b in e4 if b["variant"] != "pilot"][:1]
    blocks = development + pilots + main
    manifest = {
        "case_hash": H([{"case_id": "sample-a"}, {"case_id": "sample-b"}]),
        "episode_cap": "2",
        "blocks": blocks,
        "rows": [r for b in blocks for r in planned_rows(b)],
    }
    write_json(args.continuation_of / "plan.json", manifest)
    # Earlier synthetic source files are irrelevant to this frozen plan.
    (args.continuation_of / "budget-cutoffs/USD.json").unlink()
    for block in development:
        write_json(
            args.continuation_of / "blocks" / (block["block_id"] + ".json"),
            {
                "rows": [
                    {
                        **r,
                        "status": "valid_completed",
                        "usage_measurements": {
                            "cost": "1/100",
                            "total_tokens": 100,
                        },
                    }
                    for r in planned_rows(block)
                ],
                "replay_passed": True,
                "block_passed": True,
            },
        )
    for block in pilots:
        write_json(
            args.continuation_of / "blocks" / (block["block_id"] + ".json"),
            {
                "rows": [{**r, "termination": "budget_truncated"} for r in planned_rows(block)],
                "replay_passed": False,
                "block_passed": False,
            },
        )
    for block in main:
        write_json(
            args.continuation_of / "blocks" / (block["block_id"] + ".json"),
            {
                "rows": planned_rows(block),
                "reason": "E4 paid pilot or remaining-budget gate",
            },
        )
    observed = mock_execution(runner, monkeypatch)
    monkeypatch.setattr(runner, "verify_e4", lambda *a: None)
    interpreter = runner.ROOT / ".tmp/agentdojo-venv/Scripts/python.exe"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_bytes(b"synthetic executable marker; subprocess is mocked")
    args.execute = True
    runner.coordinate(args)
    assert [s["row"] for s in observed["record"]] == [
        r for b in pilots + main for r in planned_rows(b)
    ]
    assert len([s for s in observed["record"] if s["row"]["variant"] == "pilot"]) == 4
