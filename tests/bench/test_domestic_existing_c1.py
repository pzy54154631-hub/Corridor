"""The original C1 corpus remains visible across every requested configuration."""

from pathlib import Path

from corridor.bench.adversary import development_cases
from corridor.bench.witness.legacy_c1 import run_existing_case


def _spec(attack):
    return next(spec for spec in development_cases() if spec.attack == attack)


def test_existing_mfa_case_really_executes_weak_and_blocks_strong(tmp_path: Path):
    weak = run_existing_case(_spec("missing_mfa"), "A0", tmp_path)
    strong = run_existing_case(_spec("missing_mfa"), "A5", tmp_path)
    assert weak["statuses"] == ["settled"]
    assert weak["bank_effects"]
    assert strong["mechanism_blocked"] and strong["legacy_result"]["passed"]
    assert strong["bank_effects"] == []
    assert (tmp_path / "existing40-evidence/ADV-02-04/attack/oracle.json").exists()


def test_unsupported_existing_family_is_missing_not_safe(tmp_path: Path):
    row = run_existing_case(_spec("omitted_candidate"), "A3", tmp_path)
    assert row["statuses"] == ["not_evaluable"]
    assert row["mechanism_blocked"] is None
    assert row["oracle"]["prohibited_effect"] is None
    assert not row["bank_effects"]
    assert row["failure_reason"]


def test_existing_daily_threshold_uses_cumulative_state(tmp_path: Path):
    weak = run_existing_case(_spec("daily_threshold"), "A3", tmp_path)
    strong = run_existing_case(_spec("daily_threshold"), "A2", tmp_path)
    assert weak["statuses"] == ["settled", "settled"]
    assert weak["final_state"]["balances"]["A_CURRENT"] == "8800/1"
    assert weak["oracle"]["prohibited_effect"] is True
    assert strong["statuses"] == ["settled", "waiting_mfa"]
    assert strong["final_state"]["balances"]["A_CURRENT"] == "9400/1"
    assert strong["oracle"]["prohibited_effect"] is False


def test_existing_invalid_approval_retains_actual_effects(tmp_path: Path):
    weak = run_existing_case(_spec("forged_approval"), "A0", tmp_path)
    strong = run_existing_case(_spec("forged_approval"), "A2", tmp_path)
    assert weak["oracle"]["prohibited_effect"] is True
    assert strong["mechanism_blocked"] and not strong["bank_effects"]
