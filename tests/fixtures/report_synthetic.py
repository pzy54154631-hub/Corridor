"""Deterministic synthetic-only inputs for the Chinese report end-to-end rehearsal.

This helper reads copied plan metadata only. It never opens blind cases, invokes
the experiment runner, calls a model, or produces a submission artifact.
"""

import argparse
import json
from collections import Counter, defaultdict
from copy import deepcopy
from hashlib import sha256
from pathlib import Path


def _reject_float(value):
    raise ValueError(f"Unexpected JSON floating point value: {value}")


def read_json(path: Path) -> dict:
    return json.loads(path.read_bytes(), parse_float=_reject_float)


def write_synthetic(path: Path, value: dict) -> None:
    if "synthetic" not in path.name or value.get("synthetic") is not True:
        raise ValueError("Synthetic rehearsal filenames and payloads must be explicit")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )


def synthetic_observation(planned: dict, *, generation: int = 0) -> dict:
    identity = [planned["block_id"], planned["config"], generation]
    number = int.from_bytes(sha256(json.dumps(identity).encode("utf-8")).digest()[:8])
    outcome = number % 20
    statuses = {
        10: "runtime_error",
        11: "runtime_error",
        12: "runtime_error",
        13: "unknown",
        14: "not_evaluable",
        15: "truncated",
        16: "not_run",
        17: "invalid_case",
        18: "unsupported_oracle",
    }
    status = statuses.get(outcome, "valid_completed")
    if planned.get("evaluability") == "not_evaluable":
        status = "not_evaluable"
    measured = status not in {"not_run", "invalid_case", "unsupported_oracle"}
    completed = status == "valid_completed" and number % 7 != 0
    harm = (number // 20) % 5 == 0 if status == "valid_completed" else None
    row = {
        **deepcopy(planned),
        "synthetic": True,
        "synthetic_notice": "SYNTHETIC REPORT REHEARSAL; NOT EXPERIMENTAL EVIDENCE",
        "status": status,
        "completed": completed,
        "prohibited_effect": harm,
        "harm": {
            name: harm and number % 4 == index if harm is not None else None
            for index, name in enumerate(("funds", "authority", "privacy", "false_claim"))
        },
        "reason": f"synthetic deterministic {status}",
        "metrics": {
            "user_turns": number % 7 + 1,
            "model_calls": number % 5 + 1,
            "tool_calls": number % 9 + 1,
            "confirmations": number % 3,
            "mfa": number % 2,
            "latency_ms": number % 5000 + 1,
            "false_blocks_of_legal_actions": int(number % 11 == 0),
            "blocked_wrong_proposals": int(number % 13 == 0),
        }
        if measured
        else {},
        "usage_measurements": {"finish_reason_length": int(outcome == 11)},
    }
    if status == "not_evaluable":
        row["evaluability"] = "not_evaluable"
    if status == "runtime_error":
        row["error_class"] = {
            10: "network_quota",
            11: "model_output_format",
            12: "apparatus_defect",
        }[outcome]
    if status == "unknown":
        row["error_class"] = "bank_unknown"
    if status == "truncated":
        row["termination"] = "tool_budget_exhausted"
        row["error_class"] = "local_resource"
    return row


def comparison_groups(blocks: list[dict]) -> list[list[dict]]:
    """Mirror the frozen grouping keys without importing or executing the runner."""
    groups = defaultdict(list)
    for block in blocks:
        key = tuple(block.get(name) for name in ("priority", "experiment", "model", "provider"))
        key += (
            block.get("user_task", block["case_id"])
            if block["experiment"] == "E4"
            else block["case_id"],
            block.get("variant") == "pilot",
        )
        groups[key].append(block)
    return list(groups.values())


def create_synthetic_source(
    plan: dict,
    output: Path,
    *,
    original_plan_sha256: str | None = None,
    generation: int = 0,
    truncated_blocks: tuple[str, ...] = (),
) -> dict:
    if "synthetic" not in output.name:
        raise ValueError("Synthetic output directory must be clearly labeled")
    plan_path = output / "synthetic-plan.json"
    synthetic_plan = {
        **deepcopy(plan),
        "synthetic": True,
        "synthetic_notice": "SYNTHETIC REPORT REHEARSAL; NOT EXPERIMENTAL EVIDENCE",
        "original_plan_sha256": original_plan_sha256,
    }
    write_synthetic(plan_path, synthetic_plan)
    by_block = defaultdict(list)
    for row in plan["rows"]:
        by_block[row["block_id"]].append(row)
    observed, error_classes = Counter(), Counter()
    rows_count = 0
    for block in plan["blocks"]:
        rows, attempts = [], []
        planned = by_block[block["block_id"]]
        if [row["config"] for row in planned] != block["configs"]:
            raise ValueError("Copied plan rows differ from block configurations")
        for index, expected in enumerate(planned):
            row = synthetic_observation(expected, generation=generation)
            if index == 0 and block["block_id"] in truncated_blocks:
                row.update(
                    status="truncated",
                    completed=False,
                    prohibited_effect=None,
                    termination="budget_truncated",
                    early_stop={"reason": "shared_budget_exhausted"},
                    reason="synthetic partial comparison budget exhaustion",
                    error_class="local_resource",
                )
            # A successful retry must not erase the first attempt's measured cost.
            if row["status"] == "valid_completed" and row["metrics"]["model_calls"] == 3:
                attempts.append({
                    **deepcopy(row),
                    "attempt": 0,
                    "status": "runtime_error",
                    "completed": False,
                    "prohibited_effect": None,
                    "harm": dict.fromkeys(row["harm"]),
                    "error_class": "model_output_format",
                    "usage_measurements": {"finish_reason_length": 1},
                })
                row["attempt"] = 1
            else:
                row["attempt"] = 0
            attempts.append(deepcopy(row))
            rows.append(row)
            observed[row["status"]] += 1
            if row.get("error_class"):
                error_classes[row["error_class"]] += 1
            rows_count += 1
        write_synthetic(
            output / "blocks" / f"{block['block_id']}.synthetic.json",
            {"synthetic": True, "block_id": block["block_id"], "rows": rows, "attempts": attempts},
        )
    return {
        "synthetic": True,
        "source": output.as_posix(),
        "plan": plan_path.as_posix(),
        "blocks": len(plan["blocks"]),
        "rows": rows_count,
        "statuses": dict(observed),
        "error_classes": dict(error_classes),
        "experiments": dict(Counter(block["experiment"] for block in plan["blocks"])),
        "providers": sorted({block["provider"] for block in plan["blocks"]}),
        "models": sorted({block["model"] for block in plan["blocks"]}),
        "configs": sorted({row["config"] for row in plan["rows"]}),
        "variants": sorted({block["variant"] for block in plan["blocks"]}),
        "seeds": sorted({block["seed"] for block in plan["blocks"]}),
    }


def create_synthetic_continuation(
    source_plan: Path, output: Path, groups: list[list[dict]]
) -> dict:
    original = read_json(source_plan)
    selected_ids = {block["block_id"] for group in groups for block in group}
    blocks = [block for block in original["blocks"] if block["block_id"] in selected_ids]
    rows = [row for row in original["rows"] if row["block_id"] in selected_ids]
    plan = {
        **original,
        "blocks": blocks,
        "rows": rows,
        "row_set": "cut-continuation",
        "source_plan_sha256": sha256(source_plan.read_bytes()).hexdigest(),
        "replaced_block_ids": [block["block_id"] for block in blocks],
        "merge_rule": "whole_block_replacement",
    }
    summary = create_synthetic_source(plan, output, generation=1)
    superseded = []
    for block in blocks:
        result = read_json(source_plan.parent / "blocks" / f"{block['block_id']}.synthetic.json")
        for row in result["rows"]:
            superseded.append({
                **row,
                "superseded_by_continuation": True,
            })
    superseded_path = output / "superseded-source-rows.synthetic.json"
    write_synthetic(superseded_path, {"synthetic": True, "rows": superseded})
    return {
        **summary,
        "comparison_groups": len(groups),
        "superseded": superseded_path.as_posix(),
        "superseded_rows": len(superseded),
    }


def create_synthetic_apparatus(output: Path) -> Path:
    payloads = {
        "intents": {
            "total": 155,
            "intent_correct": 131,
            "clarification_correct": 127,
            "planner_mode": "synthetic",
            "model": "synthetic-apparatus",
            "parameter_exact_match": {"accuracy": "119/155"},
            "parameter_accuracy": {
                "intent": {
                    "accuracy": "131/155",
                    "present_accuracy": "131/155",
                    "present_total": 155,
                }
            },
        },
        "consistency": {
            "mode": "synthetic",
            "fixed_subset": ["synthetic-consistency-case"],
            "summary": {
                "different_path_same_effect": {"numerator": 3, "denominator": 16},
                "completion": {"numerator": 13, "denominator": 16},
            },
            "proposal_conditioned": {"same_proposal_hashes": True},
        },
        "e4_gate": {"formal_e4_gate": True, "conditions": {"synthetic_calibration": True}},
        "e4_groundtruth": {
            "mode": "synthetic-model-independent-groundtruth",
            "model_calls": 0,
            "formal_result": False,
            "pairs": 144,
        },
    }
    mapping = {}
    for kind, payload in payloads.items():
        path = output / f"synthetic-{kind}.json"
        write_synthetic(path, {"synthetic": True, **payload})
        mapping[kind] = path.resolve().as_posix()
    manifest = output / "synthetic-apparatus.json"
    # The manifest schema accepts only apparatus keys; filenames carry the marker.
    manifest.write_text(json.dumps(mapping, sort_keys=True), encoding="utf-8")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live6-plan", type=Path, required=True)
    parser.add_argument("--live7-plan", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    plan6, plan7 = read_json(args.live6_plan), read_json(args.live7_plan)
    groups = comparison_groups(plan6["blocks"])
    selected = []
    for experiment in ("pilot", "E1", "E4"):
        selected.append(
            next(
                group
                for group in groups
                if group[0]["experiment"] == experiment
                and group[0]["provider"] == "openai"
                and group[0].get("variant") != "pilot"
            )
        )
    sources = [
        create_synthetic_source(
            plan6,
            args.output / "synthetic-live6",
            original_plan_sha256=sha256(args.live6_plan.read_bytes()).hexdigest(),
            truncated_blocks=tuple(group[0]["block_id"] for group in selected),
        ),
        create_synthetic_source(
            plan7,
            args.output / "synthetic-live7",
            original_plan_sha256=sha256(args.live7_plan.read_bytes()).hexdigest(),
        ),
    ]
    sources.append(
        create_synthetic_continuation(
            Path(sources[0]["plan"]), args.output / "synthetic-continuation", selected
        )
    )
    apparatus = create_synthetic_apparatus(args.output)
    summary = {"synthetic": True, "sources": sources, "apparatus": apparatus.as_posix()}
    write_synthetic(args.output / "synthetic-rehearsal-inputs.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
