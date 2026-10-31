"""The witness gate measures real bank effects and independent oracle results."""

import ast
from pathlib import Path

from corridor.bench.witness import run_witness
from corridor.bench.witness.c1 import additional_cases, run_c1_case
from corridor.schemas.base import loads_exact


def test_all_five_witness_classes_and_c1_pass(tmp_path: Path) -> None:
    result = run_witness(tmp_path)
    assert result["passed"], result
    assert {row["witness_class"] for row in result["witnesses"]} == {1, 2, 3, 4, 5}
    assert all(row["input_hash"] and "oracle" in row for row in result["witnesses"])
    assert len(result["c1_additions"]) >= 8
    assert len(result["c1_existing"]) == 40
    matrix = loads_exact((tmp_path / "c1-existing40-matrix.json").read_bytes())
    assert len(matrix["rows"]) == 400
    assert len({row["case_id"] for row in matrix["rows"]}) == 40
    assert all(row["legacy_result"]["passed"] for row in result["c1_existing"])


def test_c1_weak_baseline_harm_and_full_defense() -> None:
    spec = additional_cases()[0]
    weak = run_c1_case(spec, "A0")
    strong = run_c1_case(spec, "A5")
    assert weak["oracle"]["prohibited_effect"]
    assert not strong["oracle"]["prohibited_effect"]


def test_production_cannot_import_ablation_or_witness() -> None:
    root = Path(__file__).resolve().parents[2] / "src/corridor"
    forbidden = ("corridor.bench.ablation", "corridor.bench.witness")
    for package in ("agent", "ui", "channels", "gateway", "kernel", "stores", "schemas", "crypto"):
        for path in (root / package).rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Import):
                    assert not any(alias.name.startswith(forbidden) for alias in node.names), path
                if isinstance(node, ast.ImportFrom):
                    assert not (node.module or "").startswith(forbidden), path
