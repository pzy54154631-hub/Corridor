"""Eight isolated mechanism checks, cached once per process; no session or model."""

from copy import deepcopy
from threading import Lock

from corridor.bench.adversary import development_cases, execute_case

ATTACKS = (
    "wrong_recipient",
    "missing_mfa",
    "replayed_approval",
    "duplicate_dispatch",
    "sponsored_selection",
    "forged_receipt",
    "stopped_plan_dispatch",
    "sandbox_file",
)
_lock = Lock()
_cached: dict | None = None


def _run() -> dict:
    specs = {case.attack: case for case in development_cases()}
    rows = []
    for name in ATTACKS:
        attack = execute_case(specs[name])
        benign = execute_case(specs[name], benign=True)
        rows.append({
            key: attack[key]
            for key in (
                "case_id",
                "family",
                "attack",
                "expected_rule",
                "rule_ids",
                "status",
                "passed",
                "oracle_violation",
                "oracle_indeterminate",
                "prohibited_effects",
                "bank_dispatches",
                "legitimate_prior_effects",
                "llm_calls",
            )
        })
        rows[-1].update(
            attack_dispatches=attack["bank_dispatches"] - attack["legitimate_prior_effects"],
            benign_completed=benign["completed"],
            benign_passed=benign["passed"],
            benign_dispatches=benign["bank_dispatches"],
        )
    return {
        "rows": rows,
        "summary": {
            "attacks": len(rows),
            "blocked": sum(row["passed"] for row in rows),
            "violations": sum(row["oracle_violation"] for row in rows),
            "prohibited_effects": sum(row["prohibited_effects"] for row in rows),
            "attack_dispatches": sum(row["attack_dispatches"] for row in rows),
            "benign_completed": sum(row["benign_completed"] for row in rows),
        },
        "baseline": {
            "source": "corridor.bench.adversary.run_adversary / development_cases",
            "command": (
                "corridor adversary --output evidence/reproduced/adversary "
                "--results evidence/reproduced/adversary-results"
            ),
            "scope": "full handwritten development suite; not computed by this endpoint",
        },
    }


def report() -> dict:
    """Called in the bounded web executor. Return a copy, including any failed checks."""
    global _cached
    with _lock:
        if _cached is None:
            _cached = _run()
        return deepcopy(_cached)
