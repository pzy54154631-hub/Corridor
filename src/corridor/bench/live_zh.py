"""R2 whole-block scheduling, immutable attempts, and separate exact budgets."""

import hashlib
import os
import time
from collections.abc import Callable, Sequence
from fractions import Fraction
from pathlib import Path
from typing import Any

from corridor.agent.llm import BudgetExceeded
from corridor.bench.domestic_metrics import attempt_union
from corridor.bench.episode_control import safe_traceback
from corridor.crypto.jcs import H, jcs_bytes
from corridor.schemas.base import loads_exact

ERROR_CLASSES = frozenset({
    "network_quota",
    "model_output_format",
    "apparatus_defect",
    "bank_unknown",
    "local_resource",
})
EPISODE_TIMEOUT_SECONDS = 600
MAX_RETRIES = 2
PILOT_SAFETY_FACTOR = Fraction(3, 2)
MAIN_CONFIGS = ["A0", "A5", "B_prompt", "A2", "A3", "A4", "A1"]
BUDGET_CAPS = {"USD": Fraction(5000), "CNY": Fraction(30000), "LOCAL": Fraction(0)}
DOMESTIC_API_PROVIDERS = frozenset({"deepseek", "zhipu", "qwen-plus"})


def _domestic_models(
    selected: str | Sequence[str] | None, probe: dict[str, Any] | None
) -> dict[str, str]:
    """Bind provider aliases to exact observed deployments, keeping the legacy alias."""
    selections = [selected] if isinstance(selected, str) else list(selected or [])
    result = {}
    for model in selections:
        matches = []
        for provider in ("deepseek", "zhipu"):
            observed = (probe or {}).get("providers", {}).get(provider, {})
            names = {r["model"] for r in observed.get("models", []) if r.get("model")}
            aliases = {provider, "deepseek-chat"} if provider == "deepseek" else {provider}
            if observed.get("gate_passed") and names:
                if model in aliases and len(names) == 1:
                    matches.append((next(iter(names)), provider))
                elif model in names:
                    matches.append((model, provider))
        if not matches and model in {"deepseek-chat", "qwen-plus"}:
            matches.append((model, "deepseek" if model == "deepseek-chat" else "qwen-plus"))
        if len(matches) != 1:
            raise ValueError(
                "Domestic deployment must be registered or an unambiguous probed model"
            )
        result.setdefault(*matches[0])
    return result


def domestic_deployment_identity(model, provider, probe):
    """Bind domestic plan identities to the observed request mode and probe."""
    observed = next(
        (
            row
            for row in (probe or {}).get("providers", {}).get(provider, {}).get("models", [])
            if row.get("model") == model
        ),
        None,
    )
    if provider not in {"deepseek", "zhipu"} or observed is None:
        return {}
    return {
        "deployment_profile": {
            **observed.get("deployment_profile", {}),
            "probe_hash": H(observed),
        }
    }


def build_plan(
    cases: list[dict[str, Any]],
    *,
    domestic_api: str | Sequence[str] | None = None,
    probe: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    domestic_models = _domestic_models(domestic_api, probe)
    cases = sorted(cases, key=lambda row: row["case_id"])
    domestic = ["qwen3.8-27b", *domestic_models]
    rows: list[dict[str, Any]] = []

    def add(
        priority, experiment, models, corpus, configs, repeats, variants=("attack", "legit_twin")
    ):
        for model in models:
            for index, case in enumerate(corpus):
                for seed in range(1, repeats + 1):
                    for variant in variants:
                        offset = index % len(configs)
                        payload = {
                            "priority": priority,
                            "experiment": experiment,
                            "model": model,
                            "provider": "local"
                            if model.startswith("qwen3")
                            else domestic_models.get(model, "openai"),
                            "corpus": "C2"
                            if experiment == "E1"
                            else "C2-stress"
                            if experiment == "stress"
                            else "benign"
                            if experiment == "E2"
                            else experiment,
                            "case_id": case["case_id"],
                            "base_task_id": case.get("base_task_id", case["case_id"]),
                            "attack_template_id": case.get("attack_template_id", "benign"),
                            "family_id": case.get("family_id", "benign"),
                            "seed": seed,
                            "variant": variant,
                            "configs": configs[offset:] + configs[:offset],
                            "mode": "live-recorded",
                            "max_retries": MAX_RETRIES,
                            "episode_timeout_seconds": EPISODE_TIMEOUT_SECONDS,
                            "invalid_case": case.get("status") == "invalid_case",
                        }
                        if experiment == "E2":
                            payload["common_capability"] = case["common_capability"]
                        payload.update(
                            domestic_deployment_identity(model, payload["provider"], probe)
                        )
                        if payload["provider"] == "zhipu":
                            # The legacy report honors in_denominator; the explicit
                            # exploratory report section retains all these rows.
                            payload.update(
                                exploratory=True, main_hypothesis=False, in_denominator=False
                            )
                        rows.append({**payload, "block_id": H(payload)[:24]})

    add(2, "E1", ["gpt-6.1-sol"], cases, MAIN_CONFIGS, 3)
    add(3, "E1", domestic, cases, ["A0", "A5", "B_prompt"], 3)
    benign = [{"case_id": f"J{i:02}"} for i in range(1, 25)]
    scenario_file = Path(__file__).resolve().parents[3] / "data/scenarios/zh/cases.json"
    scenarios = [{"case_id": row["id"]} for row in loads_exact(scenario_file.read_bytes())]
    from corridor.bench.interaction import COMMON_TRANSFERS

    for case in benign + scenarios:
        case["common_capability"] = case["case_id"] in COMMON_TRANSFERS
    add(
        4,
        "E2",
        ["gpt-6.1-sol", "qwen3.8-27b"],
        benign + scenarios,
        ["A0", "A5"],
        3,
        ("legit_twin",),
    )
    add(5, "E1", ["qwen3-8b", "qwen3.5-4b", "gpt-5.4-mini"], cases, ["A0", "A5"], 3)
    # A0/A5 controls already have these exact identities in the main comparison.
    add(6, "E1", ["gpt-6.1-sol"], cases, ["B_guard"], 3)
    add(
        7,
        "E3",
        ["gpt-6.1-sol", "qwen3.8-27b"],
        benign[:8] + scenarios[:8],
        ["A0", "A5"],
        5,
        ("benign",),
    )
    counts: dict[str, int] = {}
    stress = []
    for case in cases:
        family = case.get("harm_class")
        if family is None:
            continue  # Invalid files remain in E1; their unknown stress stratum is not invented.
        if counts.get(family, 0) < 2:
            stress.append(case)
            counts[family] = counts.get(family, 0) + 1
    add(
        8,
        "stress",
        ["qwen3.8-27b", "qwen3-8b", "qwen3.5-4b"],
        stress,
        ["A0", "A5"],
        10,
        ("attack",),
    )
    # E4 has its own exact 16 + 144 manifest, including registered sensitivity rows.
    from corridor.bench.agentdojo_contract import e4_blocks

    rows.extend(e4_blocks())
    return rows


def build_pilot_plan(samples: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Four R5 development episodes, separate from every formal denominator."""
    if len(samples) != 2 or any(case.get("status") == "invalid_case" for case in samples):
        raise ValueError("R5 pilot requires exactly two valid development samples")
    result = []
    for case in sorted(samples, key=lambda row: row["case_id"]):
        payload = {
            "priority": 1,
            "experiment": "pilot",
            "corpus": "development_sample",
            "model": "gpt-6.1-sol",
            "provider": "openai",
            "case_id": case["case_id"],
            "seed": 1,
            "variant": "attack",
            "configs": ["A0", "A5"],
            "mode": "live-recorded",
            "in_denominator": False,
        }
        result.append({**payload, "block_id": H(payload)[:24]})
    return result


def comparison_groups(blocks: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Keep every case's stage/configuration/variant/seed comparison together."""
    groups: dict[tuple, list[dict[str, Any]]] = {}
    for block in blocks:
        identity = tuple(
            block.get(key) for key in ("priority", "experiment", "model", "provider")
        ) + (
            block.get("user_task", block["case_id"])
            if block["experiment"] == "E4"
            else block["case_id"],
            block.get("variant") == "pilot",
        )
        groups.setdefault(identity, []).append(block)
    return list(groups.values())


def schedule_blocks(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep each local deployment consecutive while preserving comparison identities.

    Nonlocal blocks retain their order (including paid pilot prerequisites). Local
    models follow in first-seen order, with their original stage and case order.
    """
    remote, local = [], {}
    for group in comparison_groups(blocks):
        first = group[0]
        if first["provider"] == "local":
            local.setdefault(first["model"], []).extend(group)
        else:
            remote.extend(group)
    return remote + [block for group in local.values() for block in group]


def build_rehearsal_plan(
    samples: list[dict[str, Any]],
    *,
    domestic_api: str | Sequence[str] | None = None,
    probe: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Small development matrix, excluded from every formal result denominator."""
    if not samples or any(case.get("status") == "invalid_case" for case in samples):
        raise ValueError("Rehearsal requires valid development samples")
    configs = ["A0", "A1", "A2", "A3", "A4", "A5", "B_prompt", "B_guard"]
    models = ["gpt-6.1-sol", "qwen3.8-27b", "qwen3-8b", "qwen3.5-4b", "gpt-5.4-mini"]
    sample = sorted(samples, key=lambda row: row["case_id"])[0]
    rows = []
    domestic_models = _domestic_models(domestic_api, probe)

    def add(experiment, model, case_id, variants, selected, *, seeds=(1,), **extra):
        for seed in seeds:
            for variant in variants:
                payload = {
                    "priority": 1,
                    "experiment": experiment,
                    "model": model,
                    "provider": "local"
                    if model.startswith("qwen3")
                    else domestic_models.get(model, "openai"),
                    "corpus": "development_sample" if experiment == "rehearsal" else "development",
                    "case_id": case_id,
                    "variant": variant,
                    "seed": seed,
                    "configs": selected,
                    "mode": "live-recorded",
                    "in_denominator": False,
                    "rehearsal": True,
                    **extra,
                }
                payload.update(domestic_deployment_identity(model, payload["provider"], probe))
                rows.append({**payload, "block_id": H(payload)[:24]})

    if domestic_api:
        for model, provider in domestic_models.items():
            add(
                "rehearsal",
                model,
                sample["case_id"],
                ("attack", "legit_twin"),
                ["A0", "A5", "B_prompt"],
                exploratory=provider == "zhipu",
            )
        return rows

    for index, model in enumerate(models):
        offset = index % len(configs)
        add(
            "rehearsal",
            model,
            sample["case_id"],
            ("attack", "legit_twin"),
            configs[offset:] + configs[:offset],
        )
    for model in (models[0], models[1]):
        for case_id in ("ZH-TR-01", "ZH-TR-03"):
            add("E2", model, case_id, ("legit_twin",), ["A0", "A5"], common_capability=True)
        add("E3", model, "ZH-TR-01", ("benign",), ["A0", "A5"], seeds=(1, 2))
        for variant, injection in (("benign", None), ("attack", "injection_task_0")):
            add(
                "E4",
                model,
                f"user_task_0:{injection or variant}",
                (variant,),
                ["A0", "A5"],
                user_task="user_task_0",
                injection_task=injection,
                attack="important_instructions",
                approval_mode="prompt-bound",
            )
    return rows


def planned_rows(block: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            **{key: value for key, value in block.items() if key != "configs"},
            "config": config,
            "status": "invalid_case" if block.get("invalid_case") else "not_run",
            "completed": False,
            "prohibited_effect": None,
            **(
                {"evaluability": "not_evaluable"}
                if block.get("experiment") == "E2"
                and block.get("common_capability") is False
                and config != "A5"
                else {}
            ),
        }
        for config in block["configs"]
    ]


def _continuation_blocks(source: Path) -> tuple[list[dict[str, Any]], str]:
    """Select only complete, recorded budget-cut comparisons from the source plan.

    Freeze-6 records the first excluded comparison per currency in budget-cutoffs,
    then writes a budget-admission report for every excluded trailing block. Both
    records are required: a marker alone must not invent unstarted source units.
    """
    raw = (source / "plan.json").read_bytes()
    plan = loads_exact(raw)
    blocks = plan.get("blocks", [])
    if not isinstance(blocks, list) or not blocks:
        raise ValueError("Continuation source has no frozen blocks")
    identities = []
    for block in blocks:
        if not isinstance(block, dict) or not isinstance(block.get("block_id"), str):
            raise ValueError("Invalid continuation block identity")
        payload = {key: value for key, value in block.items() if key != "block_id"}
        if H(payload)[:24] != block["block_id"]:
            raise ValueError("Continuation source block differs from its frozen identity")
        identities.append(block["block_id"])
    if len(set(identities)) != len(identities):
        raise ValueError("Continuation source has duplicate block identities")
    if plan.get("rows") != [row for block in blocks for row in planned_rows(block)]:
        raise ValueError("Continuation source rows differ from its frozen blocks")

    groups = comparison_groups(blocks)
    group_ids = [H([block["block_id"] for block in group])[:24] for group in groups]
    provider_currency = {
        "openai": "USD",
        "domestic": "CNY",  # The original freeze-6 provider label.
        **{provider: "CNY" for provider in DOMESTIC_API_PROVIDERS},
        "local": "LOCAL",
    }
    currencies = [provider_currency[group[0]["provider"]] for group in groups]
    cutoffs = {}
    for path in sorted((source / "budget-cutoffs").glob("*.json")):
        entry = loads_exact(path.read_bytes())
        currency, identity = entry.get("currency"), entry.get("comparison_block_id")
        if (
            currency not in {"USD", "CNY"}
            or path.stem != currency
            or currency in cutoffs
            or identity not in group_ids
        ):
            raise ValueError("Invalid continuation budget cutoff identity")
        index = group_ids.index(identity)
        if currencies[index] != currency:
            raise ValueError("Continuation budget cutoff currency differs from its group")
        cutoffs[currency] = index

    # An admitted pilot can spend the final allowance before the coordinator
    # reaches admission for the next group, so no explicit cutoff file exists.
    for index, group in enumerate(groups):
        currency = currencies[index]
        if currency not in {"USD", "CNY"}:
            continue
        for block in group:
            path = source / "blocks" / (block["block_id"] + ".json")
            if not path.is_file():
                continue
            report = loads_exact(path.read_bytes())
            if any(
                row.get("termination") == "budget_truncated"
                or row.get("early_stop", {}).get("reason") == "shared_budget_exhausted"
                for row in report.get("rows", []) + report.get("attempts", [])
            ):
                cutoffs[currency] = min(index, cutoffs.get(currency, index))

    selected = set()
    for index, group in enumerate(groups):
        retained = []
        partial_budget = False
        for block in group:
            path = source / "blocks" / (block["block_id"] + ".json")
            if not path.is_file():
                continue
            report = loads_exact(path.read_bytes())
            partial_budget |= any(
                row.get("termination") == "budget_truncated"
                or row.get("early_stop", {}).get("reason") == "shared_budget_exhausted"
                for row in report.get("rows", []) + report.get("attempts", [])
            )
            if (
                report.get("reason") == "E4 paid pilot or remaining-budget gate"
                and block["experiment"] == "E4"
                and block["provider"] == "openai"
                and block["variant"] != "pilot"
                and "USD" in cutoffs
                and index >= cutoffs["USD"]
            ):
                if report.get("rows") != planned_rows(block) or report.get("attempts"):
                    raise ValueError("Continuation E4 cutoff report differs from its frozen block")
                retained.append(block["block_id"])
                continue
            if report.get("reason") != "Complete case comparison budget admission":
                continue
            if (
                currencies[index] not in cutoffs
                or index < cutoffs[currencies[index]]
                or report.get("comparison_block_id") != group_ids[index]
                or report.get("rows") != planned_rows(block)
                or report.get("attempts")
            ):
                raise ValueError("Continuation cutoff report differs from its frozen block")
            retained.append(block["block_id"])
        if partial_budget:
            selected.update(block["block_id"] for block in group)
            continue
        if retained and len(retained) != len(group):
            raise ValueError("Continuation cutoff comparison group is incomplete")
        if index in cutoffs.values() and len(retained) != len(group):
            raise ValueError("Continuation cutoff comparison group is incomplete")
        selected.update(retained)
    return [block for block in blocks if block["block_id"] in selected], hashlib.sha256(
        raw
    ).hexdigest()


def select_row_set(
    blocks: list[dict[str, Any]],
    *,
    row_set: str = "all",
    continuation_of: Path | None = None,
    rerun_manifest: Path | None = None,
    require_final: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Keep selected block order and bind continuation to exact source-plan bytes."""
    if row_set not in {"all", "domestic-api", "cut-continuation", "rerun"}:
        raise ValueError("Unknown row set")
    if (row_set == "rerun") != (rerun_manifest is not None):
        raise ValueError("rerun requires a rerun manifest exclusively")
    if row_set == "rerun":
        if continuation_of is not None:
            raise ValueError("Rerun cannot mix continuation sources")
        from corridor.bench.rerun_zh import load_manifest

        return load_manifest(rerun_manifest, require_final=require_final)
    if (row_set == "cut-continuation") != (continuation_of is not None):
        raise ValueError("cut-continuation requires a continuation source exclusively")
    source_hash = None
    if continuation_of is not None:
        selected, source_hash = _continuation_blocks(continuation_of)
    elif row_set == "domestic-api":
        selected = [block for block in blocks if block["provider"] in DOMESTIC_API_PROVIDERS]
    else:
        selected = blocks
    return selected, {"row_set": row_set, "source_plan_sha256": source_hash}


def report_row_sets(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Separate exploratory, pilot, auxiliary and excluded observations explicitly."""
    sections: dict[str, dict[str, Any]] = {
        "preregistered": {"included_in_main_hypotheses": True, "rows": []},
        "exploratory": {"included_in_main_hypotheses": False, "rows": []},
        "pilots": {"included_in_main_hypotheses": False, "rows": []},
        "auxiliary": {"included_in_main_hypotheses": False, "rows": []},
        "excluded": {"included_in_main_hypotheses": False, "rows": []},
        "superseded": {"included_in_main_hypotheses": False, "rows": []},
    }
    for row in rows:
        exploratory = row.get("provider") == "zhipu" or row.get("exploratory") is True
        section = (
            "superseded"
            if row.get("superseded_by_continuation")
            else "exploratory"
            if exploratory
            else "pilots"
            if row.get("experiment") == "pilot" or row.get("variant") == "pilot"
            else "auxiliary"
            if row.get("experiment") in {"E3", "E4", "stress"}
            or row.get("corpus") in {"E3", "C3", "C2-stress"}
            else "excluded"
            if row.get("in_denominator") is False
            else "preregistered"
        )
        sections[section]["rows"].append(row)
    return sections


def replay_projection(value: Any) -> Any:
    """Exclude provenance/measurement labels, retaining decisions and effects."""
    excluded = {
        "mode",
        "planner_mode",
        "planner",
        "call_sources",
        "latency_ms",
        "elapsed_ms",
        "returned_model",
        "usage_measurements",
    }
    if isinstance(value, dict):
        return {
            key: replay_projection(
                {
                    name: measurement
                    for name, measurement in child.items()
                    if name
                    not in {
                        "api_calls",
                        "input_tokens",
                        "output_tokens",
                        "reasoning_tokens",
                        "total_tokens",
                        "cost_usd",
                    }
                }
                if key == "metrics" and isinstance(child, dict)
                else child
            )
            for key, child in value.items()
            if key not in excluded
        }
    if isinstance(value, list):
        return [replay_projection(child) for child in value]
    return value


def _append_progress(path: Path, entry: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as stream:
        stream.write(jcs_bytes(entry) + b"\n")
        stream.flush()
        os.fsync(stream.fileno())


def execute_block(
    block: dict[str, Any],
    run: Callable,
    replay: Callable,
    *,
    progress: Path | None = None,
    recover: Callable | None = None,
) -> dict[str, Any]:
    """Replay every attempt and retry only network/quota failures of one identity."""
    rows, attempts = [], []
    events = (
        [loads_exact(line) for line in progress.read_bytes().splitlines()]
        if progress and progress.exists()
        else []
    )
    if any(event["block_id"] != block["block_id"] for event in events):
        raise ValueError("Progress changed the frozen comparison block")

    def save(event):
        if progress is not None:
            _append_progress(progress, {"block_id": block["block_id"], **event})

    def invoke(callback, planned, attempt):
        try:
            return callback(planned, attempt)
        except BudgetExceeded:
            return {"status": "truncated", "reason": "budget_exceeded"}
        except Exception as error:
            # Preserve the attempted unit and continue unrelated configurations.
            # Exception messages may contain transport details, so keep only the type.
            return {
                "status": "runtime_error",
                "error_class": "apparatus_defect",
                "reason": type(error).__name__,
                "traceback": safe_traceback(error),
            }

    for planned in planned_rows(block):
        if planned["status"] == "invalid_case":
            rows.append(planned)
            continue
        final = planned
        for attempt in range(MAX_RETRIES + 1):
            previous = [
                event
                for event in events
                if event.get("config") == planned["config"] and event.get("attempt") == attempt
            ]
            finished = next(
                (event for event in reversed(previous) if event["state"] == "finished"), None
            )
            if finished is not None:
                final = finished["row"]
            elif previous:
                # A killed process may already have sent a request. Never duplicate it.
                recovered = invoke(recover, planned, attempt) if recover is not None else None
                final = (
                    {**planned, **recovered}
                    if recovered is not None
                    else {
                        **planned,
                        "status": "unknown",
                        "error_class": "bank_unknown",
                        "reason": "interrupted_in_flight",
                    }
                )
                save({
                    "config": planned["config"],
                    "attempt": attempt,
                    "state": "finished",
                    "row": final,
                })
            else:
                save({"config": planned["config"], "attempt": attempt, "state": "started"})
                final = {**planned, **invoke(run, planned, attempt)}
                save({
                    "config": planned["config"],
                    "attempt": attempt,
                    "state": "finished",
                    "row": final,
                })
            if any(final[key] != planned[key] for key in ("case_id", "seed", "config")):
                raise ValueError("Retry changed the frozen case/seed/config identity")
            category = final.get("error_class")
            if category is not None and category not in ERROR_CLASSES:
                raise ValueError("Unknown failure category")
            attempts.append({**final, "attempt": attempt})
            if final["status"] != "runtime_error" or category != "network_quota":
                break
        rows.append(
            attempt_union(final, [row for row in attempts if row["config"] == planned["config"]])
        )
    # This callback runs before the scheduler can start another comparison block.
    replay_error = None
    replay_traceback = None
    try:
        verified = bool(replay(attempts))
    except Exception as error:
        verified = False
        replay_error = type(error).__name__
        replay_traceback = safe_traceback(error)
    return {
        "block_id": block["block_id"],
        "rows": rows,
        "attempts": attempts,
        "replay_passed": verified,
        "block_passed": verified
        and all(row["status"] not in {"runtime_error", "unknown"} for row in rows),
        "replay_error": replay_error,
        "replay_traceback": replay_traceback,
    }


def estimate_group_cost(
    pilot_mean_cost: Fraction,
    planned_attempts: int,
    *,
    safety_factor: Fraction = PILOT_SAFETY_FACTOR,
) -> Fraction:
    """R5 planning estimate, separate from the actual spend enforcement journal."""
    mean, factor = Fraction(pilot_mean_cost), Fraction(safety_factor)
    if mean < 0 or factor < 1 or type(planned_attempts) is not int or planned_attempts < 0:
        raise ValueError("Invalid pilot cost estimate")
    return mean * factor * planned_attempts


class BudgetJournal:
    """Append-only planning reservations, denominated in one currency.

    Reserve a pilot-based estimate for each complete case comparison, without
    retries. Sequential admission uses actual remaining spend; old reservations
    remain as audit evidence rather than accumulating unspent planning padding.
    """

    def __init__(self, path: Path, currency: str, cap: Fraction) -> None:
        if currency not in {"USD", "CNY", "LOCAL"} or cap < 0:
            raise ValueError("Invalid budget currency or cap")
        self.path, self.currency, self.cap = path, currency, cap

    def remaining(self) -> Fraction:
        spent = Fraction(0)
        if self.path.exists():
            for line in self.path.read_bytes().splitlines():
                entry = loads_exact(line)
                if entry["currency"] != self.currency or Fraction(entry["cap"]) != self.cap:
                    raise ValueError("Budget journal currency or cap mismatch")
                amount = Fraction(entry["reserved"])
                if amount < 0:
                    raise ValueError("Negative budget reservation")
                spent += amount
        return self.cap - spent

    def reserve(
        self, block_id: str, amount: Fraction, *, available: Fraction | None = None
    ) -> None:
        self.remaining()  # Validate every existing entry even for an idempotent reservation.
        if self.path.exists():
            for line in self.path.read_bytes().splitlines():
                entry = loads_exact(line)
                if entry["block_id"] == block_id:
                    if Fraction(entry["reserved"]) != amount:
                        raise ValueError("Budget reservation identity mismatch")
                    return
        if amount < 0 or amount > (self.remaining() if available is None else available):
            raise ValueError("Whole-block budget exhausted")
        entry = {
            "schema_version": "zh-currency-budget-v1",
            "block_id": block_id,
            "currency": self.currency,
            "cap": str(self.cap),
            "reserved": str(amount),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("ab") as stream:
            stream.write(jcs_bytes(entry) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())


class SpendingJournal:
    """Append-only actual-cost ledger for a sequential coordinator.

    Each request attempt holds a bounded allowance before any provider call. A
    completed attempt releases the unused allowance only when its exact measured
    cost is available. Interrupted or unmeasured attempts retain their full hold.
    The coordinator's exclusive run lock prevents concurrent ledger writers.
    """

    def __init__(self, path: Path, currency: str, cap: Fraction) -> None:
        if currency not in BUDGET_CAPS or cap < 0:
            raise ValueError("Invalid budget currency or cap")
        self.path, self.currency, self.cap = path, currency, Fraction(cap)

    def _entries(self) -> dict[str, dict[str, Fraction]]:
        attempts: dict[str, dict[str, Fraction]] = {}
        if not self.path.exists():
            return attempts
        committed = Fraction(0)
        frozen = False
        for line in self.path.read_bytes().splitlines():
            entry = loads_exact(line)
            if entry["currency"] != self.currency or Fraction(entry["cap"]) != self.cap:
                raise ValueError("Spending journal currency or cap mismatch")
            identity = entry["attempt_id"]
            amount = Fraction(entry["amount"])
            if entry["state"] == "held":
                if identity in attempts or amount < 0:
                    raise ValueError("Invalid spending hold identity")
                attempts[identity] = {"held": amount}
                committed += amount
            elif entry["state"] in {"settled", "overrun"}:
                if (
                    identity not in attempts
                    or "spent" in attempts[identity]
                    or amount < 0
                    or (entry["state"] == "overrun") != (amount > attempts[identity]["held"])
                ):
                    raise ValueError("Invalid spending settlement identity")
                attempts[identity]["spent"] = amount
                committed -= attempts[identity]["held"] - amount
                if entry["state"] == "overrun":
                    attempts[identity]["overrun"] = Fraction(1)
                    frozen = True
            else:
                raise ValueError("Invalid spending journal state")
            if committed > self.cap and not frozen:
                raise ValueError("Spending journal exceeds budget cap")
        return attempts

    def remaining(self) -> Fraction:
        attempts = self._entries()
        if any("overrun" in row for row in attempts.values()):
            return Fraction(0)
        return self.cap - sum(
            (row.get("spent", row["held"]) for row in attempts.values()), Fraction(0)
        )

    def begin(self, attempt_id: str, ceiling: Fraction = Fraction(2)) -> Fraction:
        ceiling = Fraction(ceiling)
        if ceiling < 0:
            raise ValueError("Invalid spending ceiling")
        attempts = self._entries()
        if any("overrun" in row for row in attempts.values()):
            if attempt_id not in attempts:
                self._append(attempt_id, "held", Fraction(0))
            return Fraction(0)
        if attempt_id in attempts:
            return attempts[attempt_id]["held"]
        remaining = self.cap - sum(
            (row.get("spent", row["held"]) for row in attempts.values()), Fraction(0)
        )
        held = min(ceiling, remaining)
        self._append(attempt_id, "held", held)
        return held

    def settle(self, attempt_id: str, actual_cost: Fraction) -> None:
        actual_cost = Fraction(actual_cost)
        attempts = self._entries()
        if attempt_id not in attempts:
            raise ValueError("Unknown spending attempt")
        attempt = attempts[attempt_id]
        if "spent" in attempt:
            if attempt["spent"] != actual_cost:
                raise ValueError("Spending settlement identity mismatch")
            if "overrun" in attempt:
                raise ValueError("Actual cost is outside reserved allowance; spending frozen")
            return
        if actual_cost < 0:
            raise ValueError("Actual cost is outside reserved allowance")
        if actual_cost > attempt["held"]:
            # A provider/accounting defect must not hide already observed spending.
            # Preserve the actual cost and disable subsequent paid attempts.
            self._append(attempt_id, "overrun", actual_cost)
            raise ValueError("Actual cost is outside reserved allowance; spending frozen")
        self._append(attempt_id, "settled", actual_cost)

    def _append(self, attempt_id: str, state: str, amount: Fraction) -> None:
        _append_progress(
            self.path,
            {
                "schema_version": "zh-actual-cost-v1",
                "attempt_id": attempt_id,
                "currency": self.currency,
                "cap": str(self.cap),
                "state": state,
                "amount": str(amount),
            },
        )


def verify_local_runtime(lock, status, model):
    """Read-only /api/ps evidence must show the exact locked model resident on GPU."""
    from corridor.bench.request_probe import REQUIRED_LOCAL_CONTEXT

    tiers = {"qwen3.8-27b": "strong", "qwen3-8b": "medium", "qwen3.5-4b": "weak"}
    locked = next((row for row in lock if row.get("tier") == tiers.get(model)), None)
    if locked is None:
        raise ValueError("Local deployment is absent from the lock")
    running = next(
        (
            row
            for row in status.get("models", [])
            if row.get("name", row.get("model")) == locked["model"]
        ),
        None,
    )
    if running is None or type(running.get("size_vram")) is not int or running["size_vram"] <= 0:
        raise ValueError("Local block stopped: locked model is not resident on GPU")
    if running.get("digest") != locked["registry_manifest_sha256"]:
        raise ValueError("Local block stopped: loaded model digest mismatch")
    if running.get("context_length") != REQUIRED_LOCAL_CONTEXT:
        raise ValueError("Local block stopped: loaded model context length mismatch")
    return {
        "verified": True,
        "model": locked["model"],
        "digest": running["digest"],
        "size_vram": running["size_vram"],
        "context_length": running["context_length"],
    }


def ensure_local_runtime(
    lock,
    model,
    *,
    load,
    status,
    timeout_seconds=180,
    monotonic=time.monotonic,
    sleep=time.sleep,
):
    """Load only via a minimal request and wait for the locked GPU deployment.

    The supplied callbacks only issue /api/generate and /api/ps requests; neither
    this helper nor the coordinator manages the Ollama service lifecycle.
    """
    if timeout_seconds <= 0:
        raise ValueError("Local model residency timeout must be positive")
    tiers = {"qwen3.8-27b": "strong", "qwen3-8b": "medium", "qwen3.5-4b": "weak"}
    locked = next((row for row in lock if row.get("tier") == tiers.get(model)), None)
    if locked is None:
        raise ValueError("Local deployment is absent from the lock")
    deadline = monotonic() + timeout_seconds
    try:
        return verify_local_runtime(lock, status(), model)
    except ValueError:
        pass
    try:
        load(locked["model"], max(1, deadline - monotonic()))
    except Exception as error:
        raise ValueError(f"Local model load request failed ({type(error).__name__})") from None
    last_reason = "locked model is not resident on GPU"
    while monotonic() < deadline:
        try:
            return verify_local_runtime(lock, status(), model)
        except ValueError as error:
            last_reason = str(error)
        remaining = deadline - monotonic()
        if remaining > 0:
            sleep(min(1, remaining))
    raise ValueError(f"Local model residency timeout: {last_reason}")


def verify_local_lock(lock: list[dict[str, Any]], status: dict[str, Any]) -> dict[str, Any]:
    if (
        not status.get("managed_process_running")
        or not status.get("loopback_only_verified")
        or status.get("api_error")
    ):
        raise ValueError("Local service ownership/loopback check failed")
    if status.get("version", {}).get("version") != "0.40.5":
        raise ValueError("Local engine version mismatch")
    available_models = {
        item["name"]: item["digest"] for item in status["available_models"]["models"]
    }
    for model in lock:
        expected = model["registry_manifest_sha256"]
        if available_models.get(model["model"]) != expected:
            raise ValueError("Local model registry digest mismatch")
        if (
            hashlib.sha256(Path(model["manifest_path"]).read_bytes()).hexdigest()
            != model["manifest_sha256"]
        ):
            raise ValueError("Local manifest digest mismatch")
        # Lock metadata verifies every immutable layer identity without rehashing tens of GB.
        if any(
            layer["digest"] != "sha256:" + layer["verified_sha256"]
            or not Path(layer["path"]).is_file()
            for layer in model["layers"]
        ):
            raise ValueError("Local model layer digest/path mismatch")
    return {
        "verified": True,
        "engine": "0.40.5",
        "lock_sha256": H(lock),
        "models": [row["model"] for row in lock],
    }
