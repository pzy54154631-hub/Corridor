"""Synthetic regressions for plan-bound, multiple-source domestic reports."""

from copy import deepcopy
from hashlib import sha256

import pytest

from corridor.crypto.jcs import jcs_bytes
from corridor.schemas.base import loads_exact
from scripts.final_report_zh import build_report, load_sources, merge_planned_rows


def synthetic_source(tmp_path, name="synthetic-base", configs=("A0", "A5")):
    source = tmp_path / name
    (source / "blocks").mkdir(parents=True)
    rows = [
        dict(
            synthetic=True,
            block_id=f"synthetic-{seed}",
            case_id="synthetic-case",
            model="synthetic-model",
            provider="synthetic-provider",
            config=config,
            seed=seed,
            mode="live-recorded",
            corpus="C2",
            variant="attack",
            experiment="E1",
            status="not_run",
            completed=False,
            prohibited_effect=None,
        )
        for seed in (1, 2, 3)
        for config in configs
    ]
    blocks = [
        {
            **{
                k: v
                for k, v in rows[(seed - 1) * len(configs)].items()
                if k not in {"config", "status", "completed", "prohibited_effect"}
            },
            "configs": list(configs),
        }
        for seed in (1, 2, 3)
    ]
    plan = {"synthetic": True, "blocks": blocks, "rows": rows}
    path = source / "synthetic-plan.json"
    path.write_bytes(jcs_bytes(plan))
    for block in blocks:
        observed = [
            {
                **row,
                "family_id": "synthetic-observed-family",
                "status": "valid_completed",
                "completed": True,
                "prohibited_effect": row["config"] == "A0",
            }
            for row in rows
            if row["block_id"] == block["block_id"]
        ]
        (source / "blocks" / f"{block['block_id']}.json").write_bytes(
            jcs_bytes({"synthetic": True, "rows": observed})
        )
    return source, path, plan


def test_unplanned_cluster_metadata_is_not_an_invented_case_id_constraint():
    planned = dict(
        model="synthetic", config="A5", mode="live-recorded", case_id="synthetic-case", seed=1
    )
    observed = {
        **planned,
        "family_id": "synthetic-family",
        "base_task_id": "synthetic-base",
        "attack_template_id": "synthetic-template",
        "status": "valid_completed",
    }
    assert merge_planned_rows([observed], [planned])[0]["family_id"] == "synthetic-family"
    for field in ("family_id", "base_task_id", "attack_template_id"):
        with pytest.raises(ValueError, match="cluster"):
            merge_planned_rows([observed], [{**planned, field: "synthetic-frozen-other"}])


def test_source_root_ignores_plan_and_worker_duplicates_and_binds_rows(tmp_path):
    source, plan, _ = synthetic_source(tmp_path)
    (source / "synthetic-worker.json").write_bytes(
        jcs_bytes({
            "rows": [
                {
                    "case_id": "synthetic-wrong",
                    "seed": 1,
                    "model": "synthetic-model",
                    "config": "A5",
                    "mode": "live-recorded",
                    "status": "unknown",
                }
            ]
        })
    )
    rows, hashes = load_sources(
        [source], [plan], run_names=["synthetic-run"], freeze_tags=["synthetic-freeze"]
    )
    assert len(rows) == 6
    assert all(row["source_run"]["run_name"] == "synthetic-run" for row in rows)
    assert all(row["source_run"]["freeze_tag"] == "synthetic-freeze" for row in rows)
    assert all(
        row["source_run"]["plan_sha256"] == sha256(plan.read_bytes()).hexdigest() for row in rows
    )
    assert plan.as_posix() in hashes
    _, evidence = build_report(rows, dryrun=True)
    assert len(evidence["source_rows"]) == 6


def test_unbound_duplicate_sources_fail_instead_of_double_counting(tmp_path):
    source, plan, _ = synthetic_source(tmp_path)
    with pytest.raises(ValueError, match="Duplicate source"):
        load_sources([source, source], [plan, plan])
    other, other_plan, _ = synthetic_source(tmp_path, "synthetic-other")
    with pytest.raises(ValueError, match="overlap"):
        load_sources([source, other], [plan, other_plan])


@pytest.mark.parametrize("damage", ["status", "partial", "empty", "orphan_attempt"])
def test_present_block_cannot_hide_observations_as_unrun(tmp_path, damage):
    source, plan, _ = synthetic_source(tmp_path)
    result = source / "blocks" / "synthetic-1.json"
    data = loads_exact(result.read_bytes())
    if damage == "status":
        del data["rows"][0]["status"]
    elif damage == "partial":
        data["rows"].pop()
    elif damage == "empty":
        data["rows"] = []
    else:
        data["attempts"] = [{**data["rows"][0], "config": "synthetic-unplanned"}]
    result.write_bytes(jcs_bytes(data))
    with pytest.raises(ValueError):
        load_sources([source], [plan])


def test_wholly_missing_block_remains_explicit_not_run(tmp_path):
    source, plan, _ = synthetic_source(tmp_path)
    (source / "blocks" / "synthetic-1.json").unlink()
    rows, _ = load_sources([source], [plan])
    assert len(rows) == 6
    assert sum(row["status"] == "not_run" for row in rows) == 2


def synthetic_continuation(tmp_path):
    source, path, plan = synthetic_source(tmp_path)
    continuation, target, _ = synthetic_source(tmp_path, "synthetic-continuation")
    plan = deepcopy(plan)
    plan.update(
        row_set="cut-continuation",
        source_plan_sha256=sha256(path.read_bytes()).hexdigest(),
        replaced_block_ids=[block["block_id"] for block in plan["blocks"]],
        merge_rule="whole_block_replacement",
    )
    target.write_bytes(jcs_bytes(plan))
    return source, path, continuation, target, plan


def test_continuation_replaces_whole_blocks_independent_of_source_order(tmp_path):
    source, path, continuation, target, _ = synthetic_continuation(tmp_path)
    rows, _ = load_sources([continuation, source], [target, path])
    active = [row for row in rows if not row.get("superseded_by_continuation")]
    old = [row for row in rows if row.get("superseded_by_continuation")]
    assert len(active) == len(old) == 6
    assert all(row["source_run"]["run_name"] == "synthetic-continuation" for row in active)
    assert all(row["source_run"]["run_name"] == "synthetic-base" for row in old)
    _, evidence = build_report(rows, dryrun=True)
    assert sum(row["planned"] for row in evidence["summaries"]) == 6
    assert len(evidence["superseded_source_rows"]) == 6


def test_archive_validates_raw_source_but_retains_attempt_union(tmp_path):
    source, path, continuation, target, _ = synthetic_continuation(tmp_path)
    archived = []
    for result in sorted((source / "blocks").glob("*.json")):
        data = loads_exact(result.read_bytes())
        data["attempts"] = [
            {**row, "status": "runtime_error", "prohibited_effect": True, "attempt": 0}
            for row in data["rows"]
        ]
        result.write_bytes(jcs_bytes(data))
        archived.extend({**row, "superseded_by_continuation": True} for row in data["rows"])
    archive = continuation / "superseded-source-rows.synthetic.json"
    archive.write_bytes(jcs_bytes({"synthetic": True, "rows": archived}))
    rows, _ = load_sources([source, continuation], [path, target])
    old = [row for row in rows if row.get("superseded_by_continuation")]
    assert len(old) == 6
    assert all(row["prohibited_effect"] is True and len(row["attempts"]) == 1 for row in old)
    archived[0]["completed"] = False
    archive.write_bytes(jcs_bytes({"synthetic": True, "rows": archived}))
    with pytest.raises(ValueError, match="archive"):
        load_sources([source, continuation], [path, target])


@pytest.mark.parametrize("change", ["missing", "hash", "block", "partial", "metadata"])
def test_continuation_cannot_silently_mix_or_erase_rows(tmp_path, change):
    source, path, continuation, target, plan = synthetic_continuation(tmp_path)
    if change == "missing":
        (continuation / "blocks" / "synthetic-1.json").unlink()
    elif change == "hash":
        plan["source_plan_sha256"] = "synthetic-wrong-hash"
    elif change == "block":
        plan["replaced_block_ids"] = ["synthetic-not-planned"]
    elif change == "partial":
        result = continuation / "blocks" / "synthetic-1.json"
        data = loads_exact(result.read_bytes())
        data["rows"].pop()
        result.write_bytes(jcs_bytes(data))
    else:
        plan["blocks"][0]["provider"] = "synthetic-changed"
    target.write_bytes(jcs_bytes(plan))
    with pytest.raises(ValueError):
        load_sources([source, continuation], [path, target])
