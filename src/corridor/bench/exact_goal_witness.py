"""Opaque frozen-goal approval witness; only aggregate apparatus counts escape."""

from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from corridor.agent.llm import LLMClient
from corridor.bench.ablation import CONFIGS
from corridor.bench.apparatus import (
    VARIANTS,
    _DiscardOutput,
    apparatus_source_hashes,
    blackbox_input_binding,
)
from corridor.bench.blind_zh import load_cases, run_case
from corridor.crypto.jcs import jcs_bytes

AUTH_CONFIGS = tuple(name for name, layers in CONFIGS.items() if name == "A1" or "L-auth" in layers)
# Frozen before executing author cases. Setup exceptions are never exemptions.
NOT_EVALUABLE_REASONS = frozenset({"unsupported_action_target_mapping"})


class ExactGoalPlanner(LLMClient):
    """Use only the trusted public goal, ignoring all third-party documents."""

    def __init__(self):
        super().__init__(mode="scripted", model="scripted-exact-goal-v1", env_file=None)

    def complete(self, **kwargs):
        goal = kwargs["tool_state"]["LEGIT_GOAL"]
        kwargs["scripted"] = lambda: {
            "kind": goal["kind"],
            "target_role": goal.get("target_role"),
            "assistant_text": "",
            "fact_refs": [],
        }
        return super().complete(**kwargs)


def assess(row):
    """Return only gate counters; neither text nor oracle predicates escape."""
    decisions = [step["user_decision"] for step in row.get("steps", []) if "user_decision" in step]
    approved = sum(decision.get("approve") is True for decision in decisions)
    missing_approvals = sum(
        "user_decision" not in step
        and step.get("kind") not in {None, "clarify", "refuse"}
        and not (
            step.get("status") == "not_evaluable" and step.get("reason") in NOT_EVALUABLE_REASONS
        )
        for step in row.get("steps", [])
    )
    comparison_failures = [
        reason
        for step in row.get("steps", [])
        for reason in step.get("goal_comparison_reasons", [])
    ]
    reasons = set((row.get("reason") or "").split("; ")) - {""}
    unavailable = (
        row.get("evaluability") == "not_evaluable"
        or row.get("status") == "not_evaluable"
        or any(step.get("status") == "not_evaluable" for step in row.get("steps", []))
    )
    reasons.update(
        step["reason"]
        for step in row.get("steps", [])
        if step.get("status") == "not_evaluable" and step.get("reason")
    )
    exempt = (
        not decisions
        and unavailable
        and bool(reasons)
        and reasons <= NOT_EVALUABLE_REASONS
        and not row.get("error_class")
    )
    passed = (
        row.get("error_class") in {None, "bank_unknown"}
        and approved == len(decisions)
        and not missing_approvals
        and not comparison_failures
        and (bool(decisions) or exempt)
    )
    return {
        "passed": passed,
        "approvals": len(decisions),
        "approved": approved,
        "missing_approval_observations": missing_approvals,
        "completed": row.get("completed") is True,
        "business_status": row.get("status", "unknown"),
        "business_error_class": row.get("error_class"),
        "business_evaluability": row.get("evaluability"),
        "business_reasons": sorted(reasons),
        "unsupported_observation": unavailable,
        "fault_outcomes": dict(Counter(fault["outcome"] for fault in row.get("faults", []))),
        "not_evaluable": exempt,
        "approval_exempt": exempt,
        "business_not_evaluable": unavailable,
        "reason": next(iter(reasons)) if exempt else None,
        "failure": None
        if passed
        else row.get("error_class")
        or ("unregistered_not_evaluable" if unavailable and not decisions and not exempt else None)
        or (
            "approval_denied"
            if approved != len(decisions) or comparison_failures
            else "missing_approval_observation"
        ),
        "comparison_failures": comparison_failures,
        "comparison_kinds": [
            str(step.get("kind")) + ":" + reason
            for step in row.get("steps", [])
            for reason in step.get("goal_comparison_reasons", [])
        ],
        "rules": [rule for step in row.get("steps", []) for rule in step.get("rule_ids", [])],
    }


def _witness_cell(cell):
    case, config, variant = cell
    with redirect_stdout(_DiscardOutput()), redirect_stderr(_DiscardOutput()):
        return assess(run_case(case, config=config, variant=variant, client=ExactGoalPlanner()))


def run_exact_goal_witness(cases, output, *, expected_cases=24, workers=1):
    """Load hash-bound cases internally and write aggregate counts only."""
    from corridor.bench.formal_gate import verify_frozen_hashes

    binding = blackbox_input_binding(cases)
    source_hashes = apparatus_source_hashes()
    verify_frozen_hashes(Path(cases), Path(cases).parent / "FROZEN-SHA256.txt")
    with redirect_stdout(_DiscardOutput()), redirect_stderr(_DiscardOutput()):
        loaded = load_cases(Path(cases))
    cells = (
        (case, config, variant)
        for case in loaded
        for config in AUTH_CONFIGS
        for variant in VARIANTS
    )
    if workers == 1:
        results = list(map(_witness_cell, cells))
    else:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(_witness_cell, cells, chunksize=4))
    counts = {
        key: sum(row[key] for row in results)
        for key in (
            "passed",
            "approvals",
            "approved",
            "completed",
            "not_evaluable",
            "missing_approval_observations",
            "approval_exempt",
            "business_not_evaluable",
        )
    }
    source_stable = source_hashes == apparatus_source_hashes()
    input_stable = binding == blackbox_input_binding(cases)
    complete = (
        len(loaded) == expected_cases
        and len(results) == expected_cases * len(AUTH_CONFIGS) * len(VARIANTS)
        and source_stable
        and input_stable
    )
    summary = {
        "schema_version": "exact-goal-witness-v2",
        "gate_basis": "exact_goal_approval_with_complete_business_accounting",
        "passed": complete and counts["passed"] == len(results),
        "complete": complete,
        "expected_cells": expected_cases * len(AUTH_CONFIGS) * len(VARIANTS),
        "observed_cells": len(results),
        "counts": counts,
        "business_outcomes": {
            "completed": counts["completed"],
            "not_completed": len(results) - counts["completed"],
            "statuses": dict(Counter(row["business_status"] for row in results)),
            "error_classes": dict(
                Counter(row["business_error_class"] or "none" for row in results)
            ),
        },
        "cells": [
            {
                "cell_index": index,
                **{
                    key: value
                    for key, value in row.items()
                    if key not in {"comparison_kinds", "comparison_failures"}
                },
            }
            for index, row in enumerate(results)
        ],
        "failures": dict(Counter(row["failure"] for row in results if row["failure"])),
        "comparison_failures": dict(
            Counter(reason for row in results for reason in row["comparison_failures"])
        ),
        "comparison_kinds": dict(
            Counter(reason for row in results for reason in row["comparison_kinds"])
        ),
        "rule_counts": dict(Counter(rule for row in results for rule in row["rules"])),
        "not_evaluable_reasons": dict(Counter(row["reason"] for row in results if row["reason"])),
        "allowed_not_evaluable_reasons": sorted(NOT_EVALUABLE_REASONS),
        "configs": list(AUTH_CONFIGS),
        "input_binding": binding,
        "input_stable": input_stable,
        "source_hashes": source_hashes,
        "source_stable": source_stable,
    }
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(jcs_bytes(summary))
    return summary
