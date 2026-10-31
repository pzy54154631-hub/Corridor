"""Scheduling and money isolation tests without live calls."""

from fractions import Fraction
from pathlib import Path

import pytest

from corridor.bench.live_zh import BudgetJournal, build_plan, execute_block, verify_local_lock

CASES = [
    {
        "case_id": f"ZH{i:02}",
        "base_task_id": "base",
        "attack_template_id": "template",
        "family_id": "family",
        "harm_class": str(i // 3),
    }
    for i in range(12)
]


def test_plan_priority_rotation_and_same_seed_blocks():
    plan = build_plan(CASES)
    assert [row["priority"] for row in plan] == sorted(row["priority"] for row in plan)
    main = [row for row in plan if row["priority"] == 2 and row["variant"] == "attack"]
    assert main[0]["configs"] == ["A0", "A5", "B_prompt", "A2", "A3", "A4", "A1"]
    second_case = next(row for row in main if row["case_id"] == "ZH01")
    assert second_case["configs"] == ["A5", "B_prompt", "A2", "A3", "A4", "A1", "A0"]
    assert {row["seed"] for row in main} == {1, 2, 3}
    pressure = [row for row in plan if row["priority"] == 8]
    assert len({row["case_id"] for row in pressure}) == 8
    assert {row["seed"] for row in pressure} == set(range(1, 11))


def test_retry_identity_error_retention_and_immediate_replay():
    block = build_plan(CASES)[0]
    calls, replayed = [], []

    def run(row, attempt):
        calls.append((row["case_id"], row["seed"], row["config"], attempt))
        return {
            **row,
            "status": "runtime_error" if attempt < 2 else "valid_completed",
            "error_class": "network_quota" if attempt < 2 else None,
        }

    result = execute_block(block, run, lambda rows: replayed.extend(rows) or True)
    assert len(result["attempts"]) == 3 * len(block["configs"])
    assert len(replayed) == 3 * len(block["configs"])
    assert {row["attempt"] for row in replayed} == {0, 1, 2}
    assert result["replay_passed"]
    assert {call[:2] for call in calls} == {(block["case_id"], block["seed"])}


def test_truncated_attempt_is_replayed_without_failing_or_retrying_block():
    block = {**build_plan(CASES)[0], "configs": ["A0", "A5"]}
    replayed = []
    result = execute_block(
        block,
        lambda row, attempt: {
            "status": "truncated",
            "reason": "tool_limit",
            "metrics": {"tool_calls": 16, "cost_usd": "1/50"},
        },
        lambda attempts: replayed.extend(attempts) or True,
    )
    assert result["block_passed"] is True
    assert len(result["attempts"]) == len(replayed) == 2
    assert all(row["reason"] == "tool_limit" for row in replayed)


def test_format_errors_are_retained_and_replayed_without_retry():
    block = {**build_plan(CASES)[0], "configs": ["A0"]}
    replayed = []
    result = execute_block(
        block,
        lambda row, attempt: {
            "status": "runtime_error",
            "error_class": "model_output_format",
        },
        lambda attempts: replayed.extend(attempts) or True,
    )
    assert len(result["attempts"]) == len(replayed) == 1
    assert result["replay_passed"] is True
    assert result["block_passed"] is False


def test_runtime_exception_is_retained_and_other_configs_are_attempted():
    block = {**build_plan(CASES)[0], "configs": ["A0", "A5"]}
    calls, replayed = [], []

    def run(row, attempt):
        calls.append(row["config"])
        if row["config"] == "A0":
            raise ValueError("mock apparatus failure")
        return {"status": "valid_completed"}

    result = execute_block(block, run, lambda attempts: replayed.extend(attempts) or True)
    assert calls == ["A0", "A5"]
    assert [row["status"] for row in replayed] == ["runtime_error", "valid_completed"]
    assert result["rows"][0]["error_class"] == "apparatus_defect"
    assert result["block_passed"] is False


def test_replay_error_returns_failed_block_instead_of_stopping_coordinator():
    block = {**build_plan(CASES)[0], "configs": ["A0"]}

    def replay(attempts):
        assert len(attempts) == 1
        raise ValueError("mock replay mismatch")

    result = execute_block(block, lambda row, attempt: {"status": "truncated"}, replay)
    assert result["replay_passed"] is False
    assert result["block_passed"] is False
    assert result["replay_error"] == "ValueError"


def test_local_budget_refusal_is_truncated_and_never_retried():
    from corridor.agent.llm import BudgetExceeded

    block = {**build_plan(CASES)[0], "configs": ["A0"]}
    calls, replayed = [], []

    def run(row, attempt):
        calls.append(attempt)
        raise BudgetExceeded("local cap; no transport call")

    result = execute_block(block, run, lambda attempts: replayed.extend(attempts) or True)
    assert calls == [0]
    assert result["block_passed"] is True
    assert replayed[0]["status"] == "truncated"
    assert replayed[0]["reason"] == "budget_exceeded"
    assert replayed[0].get("error_class") is None


def test_unknown_and_apparatus_errors_are_never_retried():
    block = {**build_plan(CASES)[0], "configs": ["A5"]}
    for status, category in (("unknown", "bank_unknown"), ("runtime_error", "apparatus_defect")):
        result = execute_block(
            block,
            lambda row, attempt: {**row, "status": status, "error_class": category},
            lambda rows: True,
        )
        assert len(result["attempts"]) == 1
        assert result["rows"][0]["prohibited_effect"] is None


def test_budget_reserves_complete_block_and_currency_cannot_mix(tmp_path):
    journal = BudgetJournal(tmp_path / "usd.jsonl", "USD", Fraction(50))
    journal.reserve("b1", Fraction(35))
    with pytest.raises(ValueError, match="exhausted"):
        journal.reserve("b2", Fraction(16))
    with pytest.raises(ValueError, match="currency"):
        BudgetJournal(tmp_path / "usd.jsonl", "CNY", Fraction(50)).remaining()
    assert journal.remaining() == 15


def test_local_models_must_match_every_locked_digest(tmp_path):
    model = tmp_path / "manifest"
    model.write_bytes(b"locked")
    import hashlib

    digest = hashlib.sha256(b"locked").hexdigest()
    lock = [
        {
            "model": "tag",
            "manifest_path": str(model),
            "manifest_sha256": digest,
            "registry_manifest_sha256": "registry",
            "layers": [],
        }
    ]
    status = {
        "managed_process_running": True,
        "loopback_only_verified": True,
        "api_error": None,
        "version": {"version": "0.40.5"},
        "available_models": {"models": [{"name": "tag", "digest": "registry"}]},
    }
    assert verify_local_lock(lock, status)["verified"]
    status["available_models"]["models"][0]["digest"] = "changed"
    with pytest.raises(ValueError, match="digest"):
        verify_local_lock(lock, status)


def test_local_worker_client_constructs_without_env_or_network():
    from scripts.run_live_zh import _profile, make_worker_client

    row = {"provider": "local", "model": "qwen3-8b", "seed": 1, "config": "A5"}
    lock = [{"tier": "medium", "model": "qwen3:8b-q4_K_M"}]
    observed = {
        "model": "qwen3:8b-q4_K_M",
        "deployment_profile": {"reasoning_effort": "none"},
        "parameters": {"seed": {"status": "supported"}},
    }
    profile = _profile(
        row, lock, {"providers": {"local": {"gate_passed": True, "models": [observed]}}}
    )
    client = make_worker_client(
        {
            "row": row,
            "client_profile": profile,
            "episode_cap": "1/10",
            "replay_dir": ".tmp/no-model-calls",
        },
        "record",
    )
    assert client.model == "qwen3:8b-q4_K_M"
    assert client.seed == 1
    assert client.api_calls == 0
    assert client.assumed_price_in == 1
    e4 = make_worker_client(
        {
            "row": {**row, "experiment": "E4"},
            "client_profile": profile,
            "episode_cap": "1/10",
            "replay_dir": ".tmp/no-model-calls",
        },
        "record",
    )
    assert e4.tool_namespace == "agentdojo-banking-v1.2.2"
    assert e4.deployment_profile["tool_budget"] == 16
    assert e4.deployment_profile["max_output_tokens"] == 512


@pytest.mark.skipif(
    not (Path(__file__).resolve().parents[2] / "docs/PREREG-DOMESTIC.md").is_file(),
    reason=(
        "the formal-run plan binds the preregistration document, which is not distributed "
        "with the source package"
    ),
)
def test_failed_preflight_preserves_all_planned_not_run_rows(tmp_path, monkeypatch):
    from argparse import Namespace
    from pathlib import Path

    from corridor.schemas.base import loads_exact
    from scripts.run_live_zh import coordinate

    monkeypatch.setattr(
        "scripts.run_live_zh.run_formal_gate",
        lambda **kw: {
            "ready": False,
            "status": "NOT READY",
            "checks": [{"name": "blind_hashes", "ready": False, "reason": "freeze missing"}],
        },
    )

    args = Namespace(
        cases=Path("tests/fixtures/blind-zh-sample"),
        output=tmp_path,
        domestic_api=None,
        execute=True,
        episode_cap="1/10",
    )
    with pytest.raises(ValueError, match="formal-gate"):
        coordinate(args)
    assert all(
        row["status"] == "not_run"
        for row in loads_exact((tmp_path / "plan.json").read_bytes())["rows"]
    )
    assert not loads_exact((tmp_path / "preflight-gate.json").read_bytes())["ready"]


def test_partial_e4_mapping_cannot_enter_formal_coordinator(tmp_path):
    import hashlib

    from scripts.run_live_zh import e4_formal_gate_ready

    (tmp_path / "adapter.py").write_text("pinned", encoding="utf-8")
    bridge = {"passed": True, "code_hashes": {"adapter.py": hashlib.sha256(b"pinned").hexdigest()}}
    assert not e4_formal_gate_ready(
        {"calibration_passed": True, "formal_e4_gate": False}, bridge, tmp_path
    )
    assert e4_formal_gate_ready(
        {"calibration_passed": True, "formal_e4_gate": True}, bridge, tmp_path
    )


def test_global_probe_gate_rejects_missing_paid_or_one_local_without_calls(monkeypatch):
    from scripts import run_live_zh

    calls = []
    monkeypatch.setattr(run_live_zh, "make_worker_client", lambda *a: calls.append(a))
    names = ["qwen3.8:27b-q4_K_M", "qwen3:8b-q4_K_M", "qwen3.5:4b-q4_K_M"]
    probe = {
        "providers": {
            "local": {
                "gate_passed": True,
                "models": [
                    {"model": name, "status": "valid_completed", "strict_replay": True}
                    for name in names
                ],
            },
            "openai": {"gate_passed": False, "models": []},
        }
    }
    with pytest.raises(ValueError, match="Mandatory request probe"):
        run_live_zh.require_global_probe_gate(probe)
    probe["providers"]["openai"] = {
        "gate_passed": True,
        "models": [
            {"model": model, "status": "valid_completed", "strict_replay": True}
            for model in ("gpt-6.1-sol", "gpt-5.4-mini")
        ],
    }
    run_live_zh.require_global_probe_gate(probe)
    probe["providers"]["local"]["models"].pop()
    with pytest.raises(ValueError, match="Mandatory request probe"):
        run_live_zh.require_global_probe_gate(probe)
    assert calls == []
