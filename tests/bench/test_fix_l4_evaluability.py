"""Synthetic evaluability regressions; no frozen author case is accessed."""

from copy import deepcopy

import pytest

from corridor.agent.llm import LLMClient
from corridor.bench.ablation import CONFIGS
from corridor.bench.blind_zh import run_case
from corridor.bench.consistency import summarize_consistency
from corridor.bench.domestic_metrics import aggregate_runs
from corridor.bench.evaluability import promote_evaluability, unavailable_reasons
from corridor.bench.interaction import run_interaction_episode
from corridor.cli.judge import run_bank_task
from scripts.final_report_zh import build_report
from tests.bench.test_episode_control import synthetic_case

ALL_CONFIGS = (*CONFIGS, "B_prompt", "B_guard")


class CrossKindPlanner(LLMClient):
    def __init__(self, kind):
        super().__init__(mode="scripted")
        self.kind = kind

    def complete(self, **kwargs):
        if "LEGIT_GOAL" in kwargs.get("tool_state", {}):
            kwargs["scripted"] = lambda: {
                "kind": self.kind,
                "target_role": "MOM" if self.kind == "transfer" else "LOCKED",
                "assistant_text": "",
                "fact_refs": [],
            }
        return super().complete(**kwargs)


def _rows():
    return [
        {
            "case_id": case,
            "seed": seed,
            "model": "synthetic",
            "config": "A0",
            "mode": "scripted",
            "corpus": "benign",
            "variant": "legit_twin",
            "status": "valid_completed",
            "completed": True,
            "prohibited_effect": False,
            "metrics": {"model_calls": 1},
        }
        for case in ("available", "unavailable", "unrun")
        for seed in (1, 2, 3)
    ]


@pytest.mark.parametrize(
    "reason",
    [
        "outside calibrated single-node write subset",
        "baseline has no order compiler for action",
        "unsupported_action_target_mapping",
    ],
)
def test_step_reason_is_promoted_by_runner(reason, monkeypatch):
    monkeypatch.setattr(
        "corridor.bench.ablation.execute_prepared",
        lambda *args, **kwargs: {"status": "not_evaluable", "reason": reason},
    )
    row = run_case(synthetic_case(1), config="A0", client=LLMClient())
    assert row["evaluability"] == "not_evaluable"
    assert row["reason"] == reason
    assert not row["completed"]


@pytest.mark.parametrize("config", ALL_CONFIGS)
@pytest.mark.parametrize("kind", ["transfer", "lock_funds", "unlock_funds"])
@pytest.mark.parametrize("variant", ["attack", "legit_twin"])
def test_cross_kind_proposal_without_trusted_amount_is_model_format(config, kind, variant):
    case = synthetic_case(1)
    case["legit_goal"].update(kind="balance_query", target_role="CURRENT")
    case["legit_goal"].pop("amount")
    case["legit_goal"]["required_outcomes"] = [{"predicate": "no_effect"}]
    row = run_case(case, config=config, client=CrossKindPlanner(kind), variant=variant)
    assert row["error_class"] == "model_output_format"
    assert row["status"] == "runtime_error"
    assert row["evaluability"] != "not_evaluable"


@pytest.mark.parametrize("config", ALL_CONFIGS)
@pytest.mark.parametrize("kind", ["transfer", "lock_funds", "unlock_funds"])
def test_legitimate_money_goal_without_amount_is_unavailable_before_planner(config, kind):
    case = synthetic_case(1)
    case["legit_goal"].update(kind=kind, target_role="MOM" if kind == "transfer" else "LOCKED")
    case["legit_goal"].pop("amount")
    client = LLMClient()
    row = run_case(case, config=config, client=client)
    assert row["evaluability"] == "not_evaluable"
    assert row["reason"] == "unsupported_action_target_mapping"
    assert row["error_class"] is None
    assert not client.calls


def test_execution_only_promotion_ignores_diagnostic_layer_and_metric_scope():
    row = {
        "status": "valid_completed",
        "steps": [{"status": "rejected", "layer_status": {"L-out": {"status": "not_evaluable"}}}],
        "fbr_observation": {"status": "not_evaluable", "reason": "metric not measured"},
    }
    assert promote_evaluability(row)["evaluability"] == "evaluated"
    row["result"] = {"steps": [{"status": "not_evaluable", "reason": "synthetic setup"}]}
    assert promote_evaluability(row)["reason"] == "synthetic setup"


def test_judge_and_interaction_promote_unavailable_setup_before_expected_assertions(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "corridor.cli.judge.run_completion",
        lambda *args, **kwargs: {
            "task_id": "J02",
            "passed": False,
            "steps": {"setup": [{"status": "not_evaluable", "reason": "synthetic setup mapping"}]},
        },
    )
    direct = run_bank_task("J02", LLMClient(), tmp_path / "direct")
    assert direct["evaluability"] == "not_evaluable"
    assert direct["reason"] == "synthetic setup mapping"
    episode = run_interaction_episode("J02", LLMClient(), "A5", 1, tmp_path / "episode")
    assert episode["evaluability"] == "not_evaluable"
    assert episode["reason"] == "synthetic setup mapping"
    assert not episode["completed"]


def test_native_gate_event_promotion_keeps_rejection_evaluable():
    ordinary = {"status": "valid_completed", "gate_events": [{"reason": "native_plan_reject"}]}
    assert promote_evaluability(ordinary)["evaluability"] == "evaluated"
    unavailable_gate = {
        "status": "valid_completed",
        "gate_events": [
            {"evaluability": "not_evaluable", "reason": "unsupported_action_target_mapping"}
        ],
    }
    assert promote_evaluability(unavailable_gate)["reason"] == "unsupported_action_target_mapping"


def test_multiple_reasons_remain_idempotent_and_are_counted_separately():
    row = {
        "status": "not_run",
        "steps": [
            {"status": "not_evaluable", "reason": reason}
            for reason in ("synthetic A", "synthetic B", "synthetic A")
        ],
    }
    promote_evaluability(row)
    assert promote_evaluability(row)["reason"] == "synthetic A; synthetic B"
    assert unavailable_reasons([row]) == {"synthetic A": 1, "synthetic B": 1}


def test_report_and_metrics_separate_unavailable_from_unrun_and_keep_costs():
    rows = _rows()
    for row in rows:
        if row["case_id"] == "unavailable":
            row.update(
                status="not_run",
                completed=False,
                steps=[{"status": "not_evaluable", "reason": "synthetic unsupported mapping"}],
            )
        elif row["case_id"] == "unrun":
            row.update(status="not_run", completed=False, prohibited_effect=None, metrics={})
    summary = aggregate_runs(rows)
    assert summary["planned"] == 9
    assert summary["not_evaluable"] == 3
    assert summary["not_evaluable_reasons"] == {"synthetic unsupported mapping": 3}
    assert summary["not_run"] == 3
    assert summary["asr_any_at_3"]["denominator"] == 2
    assert summary["bcr"]["per_attempt"] == "3/6"
    assert summary["interaction"]["all_task_truncated_cost"]["model_calls"] == 6
    text, report = build_report(rows, dryrun=True)
    assert report["summaries"][0]["not_run"] == 3
    assert report["summaries"][0]["not_evaluable"] == 3
    assert report["not_evaluable"][0]["reasons"] == {"synthetic unsupported mapping": 3}
    assert "synthetic unsupported mapping" in text


def test_partial_non_evaluable_cluster_is_not_recounted_as_missing_repeat():
    rows = _rows()[:3]
    rows[0].update(evaluability="not_evaluable", reason="synthetic mapping", status="not_run")
    summary = aggregate_runs(rows)
    assert summary["not_run"] == 0
    assert summary["not_evaluable"] == 1
    assert summary["not_evaluable_cases"] == 0
    assert summary["partially_evaluable_cases"] == 1
    assert summary["asr_any_at_3"]["denominator"] == 1
    assert summary["asr_any_at_3"]["unresolved_cases"] == 1


def test_unavailable_repeat_cannot_hide_evaluable_adverse_observation():
    rows = _rows()[:3]
    rows[0].update(evaluability="not_evaluable", reason="synthetic mapping", status="not_run")
    rows[1].update(prohibited_effect=True, harm={"funds": True})
    summary = aggregate_runs(rows)
    assert summary["asr_any_at_3"]["events"] == 1
    assert summary["asr_any_at_3"]["bounds"] == ["1/1", "1/1"]
    assert summary["strata"]["harm_category"]["funds"]["events"] == 1
    _, report = build_report(rows, dryrun=True)
    assert report["summaries"][0]["asr_any_at_3"]["events"] == 1


def test_consistency_excludes_unavailable_runs_and_reports_reason():
    rows = deepcopy(_rows()[:3])
    for row in rows:
        row.update(tool_path=[], business_effects=[])
    rows[0].update(
        status="not_run",
        completed=False,
        steps=[{"status": "not_evaluable", "reason": "synthetic mapping"}],
    )
    summary = summarize_consistency(rows)
    assert summary["completion"] == {"numerator": 2, "denominator": 2}
    assert summary["not_run"] == 0
    assert summary["not_evaluable"] == 1
    assert summary["not_evaluable_reasons"] == {"synthetic mapping": 1}
