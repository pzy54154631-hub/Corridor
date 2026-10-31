"""The report rehearsal covers frozen identities without opening blind cases."""

from hashlib import sha256
from pathlib import Path

from tests.fixtures.report_synthetic import create_synthetic_source, read_json


def _plan():
    blocks, rows = [], []
    for seed in range(1, 4):
        block = {
            "block_id": f"synthetic-block-{seed}",
            "case_id": "synthetic-case",
            "experiment": "E1",
            "corpus": "C2",
            "model": "synthetic-model",
            "provider": "synthetic-provider",
            "priority": 2,
            "mode": "live-recorded",
            "variant": "attack",
            "seed": seed,
            "configs": ["A0", "A5"],
        }
        blocks.append(block)
        rows.extend(
            {
                **{key: value for key, value in block.items() if key != "configs"},
                "config": config,
                "status": "not_run",
            }
            for config in block["configs"]
        )
    return {"blocks": blocks, "rows": rows, "schema_version": "synthetic-test-plan"}


def test_synthetic_source_preserves_every_frozen_identity_and_is_deterministic(tmp_path):
    plan = _plan()
    first = create_synthetic_source(plan, tmp_path / "synthetic-first")
    second = create_synthetic_source(plan, tmp_path / "synthetic-second")
    assert first["blocks"] == second["blocks"] == 3
    assert first["rows"] == second["rows"] == 6
    first_files = sorted((tmp_path / "synthetic-first/blocks").glob("*.json"))
    second_files = sorted((tmp_path / "synthetic-second/blocks").glob("*.json"))
    assert [p.read_bytes() for p in first_files] == [p.read_bytes() for p in second_files]
    expected = {(r["block_id"], r["config"]) for r in plan["rows"]}
    actual = set()
    for path in first_files:
        assert "synthetic" in path.name
        payload = read_json(path)
        assert payload["synthetic"] is True
        for row in payload["rows"]:
            assert row["synthetic"] is True
            actual.add((row["block_id"], row["config"]))
    assert actual == expected
    retained_plan = read_json(Path(first["plan"]))
    assert retained_plan["rows"] == plan["rows"]
    assert retained_plan["blocks"] == plan["blocks"]


def test_synthetic_replacement_changes_whole_group_not_only_failed_row(tmp_path):
    from tests.fixtures.report_synthetic import create_synthetic_continuation

    plan = _plan()
    source = create_synthetic_source(plan, tmp_path / "synthetic-source")
    continuation = create_synthetic_continuation(
        Path(source["plan"]), tmp_path / "synthetic-continuation", [plan["blocks"]]
    )
    assert continuation["blocks"] == 3
    assert continuation["rows"] == continuation["superseded_rows"] == 6
    retained = read_json(Path(continuation["plan"]))
    assert retained["row_set"] == "cut-continuation"
    superseded = read_json(Path(continuation["superseded"]))["rows"]
    assert all(row["superseded_by_continuation"] is True for row in superseded)
    assert {row["config"] for row in superseded} == {"A0", "A5"}
    assert {row["seed"] for row in superseded} == {1, 2, 3}


def test_synthetic_runner_shaped_archive_merges_with_separate_deployment(tmp_path):
    from scripts.final_report_zh import build_report, load_sources
    from tests.fixtures.report_synthetic import create_synthetic_continuation

    original = _plan()
    other = _plan()
    for row in other["blocks"] + other["rows"]:
        row["model"] = "synthetic-second-model"
        row["block_id"] += "-second"
    first = create_synthetic_source(original, tmp_path / "synthetic-first")
    second = create_synthetic_source(other, tmp_path / "synthetic-second")
    continuation = create_synthetic_continuation(
        Path(first["plan"]), tmp_path / "synthetic-continuation", [original["blocks"]]
    )
    sources = [first, second, continuation]
    rows, hashes = load_sources(
        [Path(source["source"]) for source in sources],
        [Path(source["plan"]) for source in sources],
        run_names=["synthetic-first", "synthetic-second", "synthetic-continuation"],
        freeze_tags=["synthetic-freeze-6", "synthetic-freeze-7", "synthetic-freeze-7"],
    )
    assert len(rows) == 18
    assert sum(row.get("superseded_by_continuation") is True for row in rows) == 6
    assert all(row["source_run"]["plan_sha256"] for row in rows)
    text, evidence = build_report(rows, dryrun=True, provenance=hashes)
    assert len(evidence["superseded_source_rows"]) == 6
    assert sum(row["planned"] for row in evidence["summaries"]) == 12
    assert "已被续跑取代" in text


def test_cli_repeated_source_bindings_and_apparatus_preserve_labels(tmp_path, monkeypatch):
    from scripts.final_report_zh import main
    from tests.fixtures.report_synthetic import (
        create_synthetic_apparatus,
        create_synthetic_continuation,
    )

    original = _plan()
    other = _plan()
    for row in other["blocks"] + other["rows"]:
        row["model"] = "synthetic-second-model"
        row["block_id"] += "-second"
    first = create_synthetic_source(original, tmp_path / "synthetic-first")
    second = create_synthetic_source(other, tmp_path / "synthetic-second")
    continuation = create_synthetic_continuation(
        Path(first["plan"]), tmp_path / "synthetic-continuation", [original["blocks"]]
    )
    sources = [first, second, continuation]
    apparatus = create_synthetic_apparatus(tmp_path / "synthetic-apparatus")
    output = tmp_path / "FINAL-RESULTS-zh-synthetic.md"
    arguments = ["final_report_zh.py"]
    expected = {}
    for index, source in enumerate(sources):
        name = f"synthetic-run-{index}"
        tag = f"synthetic-freeze-{index}"
        arguments.extend([
            "--source",
            source["source"],
            "--planned",
            source["plan"],
            "--run-name",
            name,
            "--freeze-tag",
            tag,
        ])
        expected[name] = (tag, sha256(Path(source["plan"]).read_bytes()).hexdigest())
    arguments.extend(["--apparatus", str(apparatus), "--dryrun", "--output", str(output)])
    monkeypatch.setattr("sys.argv", arguments)
    main()
    evidence = read_json(output.with_suffix(".json"))
    text = output.read_text(encoding="utf-8")
    assert text.startswith("# D3 装置干跑（非正式实验）\n")
    assert all(
        heading in text
        for heading in ("## 配对分析", "## E2 共同能力", "## E3 / E4", "## 已被续跑取代")
    )
    assert len(evidence["source_rows"]) == 18
    assert len(evidence["superseded_source_rows"]) == 6
    assert {row["source_run"]["run_name"] for row in evidence["source_rows"]} == set(expected)
    for row in evidence["source_rows"]:
        metadata = row["source_run"]
        assert (metadata["freeze_tag"], metadata["plan_sha256"]) == expected[metadata["run_name"]]
    assert evidence["apparatus"]["intents"]["total"] == 155
    assert evidence["apparatus"]["consistency"]["summary"]["completion"]["denominator"] == 16
