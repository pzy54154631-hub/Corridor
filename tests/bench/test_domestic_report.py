"""C1 mechanism evidence cannot silently become repeated C2 statistics."""

import importlib.util
from pathlib import Path

import pytest

from corridor.bench.domestic_metrics import aggregate_runs
from corridor.crypto.jcs import jcs_bytes


def _report():
    path = Path(__file__).resolve().parents[2] / "scripts/final_report_zh.py"
    spec = importlib.util.spec_from_file_location("final_report_zh_review", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_c1_mechanism_rows_are_separate_from_case_level_asr():
    rows = [
        {
            "case_id": "C1-01",
            "seed": 1,
            "model": "scripted",
            "config": "A5",
            "mode": "scripted",
            "corpus": "C1",
            "status": "valid_completed",
            "completed": False,
            "prohibited_effect": False,
            "mechanism_blocked": True,
        }
    ]
    text, evidence = _report().build_report(rows, dryrun=True)
    assert evidence["summaries"] == []
    assert evidence["c1_mechanism"][0]["evaluated"] == 1
    assert evidence["c1_mechanism"][0]["blocked"] == 1
    assert "C1/scripted/A5/scripted" not in text


def test_gate_and_model_independent_groundtruth_stay_outside_c2(tmp_path):
    gate = {"status": "NOT READY", "ready": False, "checks": []}
    groundtruth = {
        "mode": "model-independent-groundtruth",
        "model_calls": 0,
        "pairs": 144,
        "formal_result": True,
    }
    e4_gate = {"formal_e4_gate": True, "conditions": {"calibrated": True}}
    for name, body in (
        ("formal_gate", gate),
        ("e4_groundtruth", groundtruth),
        ("e4_gate", e4_gate),
    ):
        (tmp_path / f"{name}.json").write_bytes(jcs_bytes(body))
    manifest = tmp_path / "apparatus.json"
    manifest.write_bytes(
        jcs_bytes({
            name: (tmp_path / f"{name}.json").as_posix()
            for name in ("formal_gate", "e4_groundtruth", "e4_gate")
        })
    )
    report = _report()
    apparatus, provenance = report.load_apparatus(manifest)
    text, evidence = report.build_report(
        [], dryrun=True, apparatus=apparatus, provenance=provenance
    )
    assert apparatus["formal_gate"] == gate
    assert apparatus["e4_groundtruth"] == groundtruth
    assert apparatus["e4_gate"] == e4_gate
    assert evidence["summaries"] == []
    assert "模型无关" in text


def test_explicit_plan_retains_entirely_missing_case():
    report = _report()
    plan = [
        {
            "case_id": case,
            "seed": seed,
            "model": "deployment",
            "config": "A5",
            "mode": "live-recorded",
            "corpus": "C2",
        }
        for case in ("planned-01", "planned-02")
        for seed in (1, 2, 3)
    ]
    observed = [
        {**row, "status": "valid_completed", "completed": True, "prohibited_effect": False}
        for row in plan[:3]
    ]
    result = aggregate_runs(report.merge_planned_rows(observed, plan))
    assert result["planned"] == 6
    assert result["not_run"] == 3
    assert result["asr_any_at_3"]["bounds"] == ["0/2", "1/2"]
    with pytest.raises(ValueError, match="outside"):
        report.merge_planned_rows([{**observed[0], "case_id": "unplanned"}], plan)
    with pytest.raises(ValueError, match="cluster"):
        report.merge_planned_rows(
            [{**observed[0], "family_id": "changed"}],
            [{**row, "family_id": "frozen"} for row in plan],
        )


def test_loader_accepts_c1_matrix_without_inventing_three_repetitions(tmp_path):
    path = tmp_path / "matrix.json"
    path.write_bytes(
        jcs_bytes({
            "schema": "c1-domestic-matrix-v1",
            "rows": [
                {
                    "case_id": "C1-01",
                    "config": "A5",
                    "mode": "scripted",
                    "statuses": ["rejected"],
                    "oracle": {"prohibited_effect": False, "completed": False},
                }
            ],
        })
    )
    report = _report()
    rows, _ = report.load_rows(path)
    assert len(rows) == 1 and rows[0]["corpus"] == "C1"
    _, evidence = report.build_report(rows, dryrun=True)
    assert evidence["c1_mechanism"][0]["planned"] == 1
    assert evidence["c1_mechanism"][0]["blocked"] == 1


def test_c1_report_retains_existing_and_additional_corpus_counts():
    rows = [
        {
            "case_id": case_id,
            "seed": 1,
            "model": "scripted",
            "config": "A5",
            "mode": "scripted",
            "corpus": "C1",
            "corpus_segment": segment,
            "status": "valid_completed",
            "mechanism_blocked": True,
            "prohibited_effect": False,
        }
        for case_id, segment in (("ADV-01-01", "existing40"), ("C1-D3-01", "additions10"))
    ]
    text, evidence = _report().build_report(rows, dryrun=True)
    assert evidence["c1_mechanism"][0]["corpus_segments"] == {"existing40": 1, "additions10": 1}
    assert "existing40" in text and "additions10" in text


def test_report_combines_actual_existing_and_additional_matrix_rows(tmp_path):
    from corridor.bench.adversary import development_cases
    from corridor.bench.witness.c1 import additional_cases, run_c1_case
    from corridor.bench.witness.legacy_c1 import run_existing_case

    spec = next(item for item in development_cases() if item.attack == "forged_approval")
    matrix = {
        "schema": "c1-domestic-matrix-v1",
        "rows": [
            run_existing_case(spec, "A2", tmp_path),
            run_c1_case(additional_cases()[0], "A2"),
        ],
    }
    path = tmp_path / "matrix.json"
    path.write_bytes(jcs_bytes(matrix))
    report = _report()
    rows, _ = report.load_rows(path)
    _, evidence = report.build_report(rows, dryrun=True)
    assert len(evidence["c1_mechanism"]) == 1
    assert evidence["c1_mechanism"][0]["planned"] == 2
    assert evidence["c1_mechanism"][0]["blocked"] == 2


def test_auxiliary_e3_evidence_cannot_enter_c2_denominator(tmp_path):
    source = tmp_path / "e3.json"
    source.write_bytes(
        jcs_bytes({
            "mode": "scripted",
            "fixed_subset": ["J01"],
            "summary": {"cases": [], "completion": {"numerator": 3, "denominator": 5}},
            "proposal_conditioned": {"same_proposal_hashes": True},
        })
    )
    manifest = tmp_path / "apparatus.json"
    manifest.write_bytes(jcs_bytes({"consistency": source.as_posix()}))
    report = _report()
    auxiliary, hashes = report.load_apparatus(manifest)
    text, evidence = report.build_report([], dryrun=True, apparatus=auxiliary, provenance=hashes)
    assert evidence["summaries"] == []
    assert evidence["apparatus"]["consistency"]["summary"]["completion"]["denominator"] == 5
    assert len(evidence["sources"]) == 2
    assert "不混入 C2" in text


def test_benign_comparison_uses_declared_common_capability_only():
    rows = [
        {
            "case_id": case,
            "seed": seed,
            "model": "scripted",
            "config": config,
            "mode": "scripted",
            "corpus": "benign",
            "variant": "legit_twin",
            "status": "valid_completed",
            "completed": case == "common",
            "prohibited_effect": None,
            "common_capability": case == "common",
            "metrics": {},
        }
        for config, case in [("A0", "common"), ("A5", "common"), ("A5", "exclusive")]
        for seed in (1, 2, 3)
    ]
    _, result = _report().build_report(rows, dryrun=True)
    pair = result["benign_common_comparisons"][0]
    assert pair["cases"] == ["common"]
    assert pair["majority_completion_difference"] == "0"
    assert pair["left"]["bcr"]["majority"] == "1/1"
    assert pair["right"]["bcr"]["majority"] == "1/1"


def test_invalid_planned_rows_are_not_reclassified_as_unrun():
    report = _report()
    planned = [
        {
            "case_id": "invalid",
            "seed": seed,
            "model": "mock",
            "config": "A5",
            "mode": "scripted",
            "status": "invalid_case",
        }
        for seed in (1, 2, 3)
    ]
    merged = report.merge_planned_rows([], planned)
    assert all(row["status"] == "invalid_case" for row in merged)
    summary = aggregate_runs(merged)
    assert summary["planned"] == summary["invalid_case"] == 3
    assert summary["not_run"] == 0


@pytest.mark.parametrize(
    "marker",
    [
        {"experiment": "pilot"},
        {"variant": "pilot"},
        {"in_denominator": False},
    ],
)
def test_pilots_are_retained_separately_without_result_denominators(marker):
    row = {
        "case_id": "pilot-one",
        "seed": 1,
        "model": "mock",
        "config": "A5",
        "mode": "scripted",
        "status": "valid_completed",
        "completed": True,
        "prohibited_effect": False,
        "metrics": {"cost_usd": "1/100"},
        **marker,
    }
    text, result = _report().build_report([row], dryrun=True)
    assert result["summaries"] == result["paired"] == result["c1_mechanism"] == []
    assert result["pilots"] == [row]
    assert "试跑" in text and "不计入" in text


def test_report_cli_accepts_orchestrator_plan_rows(tmp_path, monkeypatch):
    report = _report()
    source, plan, output = (tmp_path / name for name in ("observed.json", "plan.json", "out.md"))
    source.write_bytes(jcs_bytes([]))
    plan.write_bytes(
        jcs_bytes({
            "rows": [
                {
                    "case_id": "invalid",
                    "seed": seed,
                    "model": "mock",
                    "config": "A5",
                    "mode": "scripted",
                    "status": "invalid_case",
                }
                for seed in (1, 2, 3)
            ]
        })
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "report",
            "--source",
            str(source),
            "--planned",
            str(plan),
            "--output",
            str(output),
            "--dryrun",
        ],
    )
    report.main()
    assert "|invalid_case|" in output.read_text(encoding="utf-8")


def test_complete_orchestrator_plan_reports_auxiliary_progress_separately():
    from corridor.bench.blind_zh import load_cases
    from corridor.bench.live_zh import build_pilot_plan, build_plan, planned_rows

    samples = load_cases(Path("tests/fixtures/blind-zh-sample"))
    plan = [
        row
        for block in build_pilot_plan(samples) + build_plan(samples)
        for row in planned_rows(block)
    ]
    report = _report()
    rows = report.merge_planned_rows([], plan)
    text, evidence = report.build_report(rows, dryrun=True)
    assert "辅助实验进度" in text
    assert {row["corpus"] for row in evidence["summaries"]} <= {"C2", "benign"}
    assert evidence["not_evaluable"]
    assert all(row["config"] == "A0" for row in evidence["not_evaluable"])
    assert all(row["corpus"] == "benign" for row in evidence["not_evaluable"])
    assert {row["corpus"] for row in evidence["auxiliary_experiments"]} == {"C2-stress", "E3", "C3"}
    assert all("asr_any_at_3" not in row for row in evidence["auxiliary_experiments"])
    assert sum(row["planned"] for row in evidence["auxiliary_experiments"]) == sum(
        len(row["rows"]) for row in evidence["auxiliary_experiments"]
    )
