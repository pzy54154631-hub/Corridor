"""Bounded interaction measurements do not mistake previews for completed work."""

import pytest

from corridor.agent.llm import LLMClient
from corridor.bench.interaction import fault_outcome, run_interaction_episode


@pytest.mark.parametrize("experiment", ["E1", "E2-common", "E2-mechanism"])
def test_recorded_episode_strict_replay_preserves_business_without_new_inference(
    experiment, tmp_path, monkeypatch
):
    import httpx

    from corridor.bench.blind_zh import load_cases, run_case
    from corridor.bench.live_zh import replay_projection
    from corridor.bench.recording import observation_hash

    def transport(self, request):
        return httpx.Response(
            200,
            json={
                "model": "gpt-6.1-sol-transport-id",
                "status": "completed",
                "output": [
                    {
                        "type": "function_call",
                        "name": "emit_result",
                        "arguments": '{"ok":true}'
                        if experiment == "E2-mechanism"
                        else '{"kind":"transfer","target_role":"MOM"}',
                    }
                ],
                "usage": {"input_tokens": 30, "output_tokens": 8, "total_tokens": 38},
            },
        )

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", transport)
    if experiment == "E2-mechanism":
        monkeypatch.setattr("corridor.cli.judge.run_bank_task", _observed_judge)
    options = dict(
        model="gpt-6.1-sol",
        api="responses",
        replay_dir=tmp_path / "recording",
        env_file=None,
        max_calls=3,
        max_usd="10",
        max_output_tokens=128,
    )

    def episode(client, path):
        if experiment == "E1":
            from pathlib import Path

            return run_case(
                load_cases(Path("tests/fixtures/blind-zh-sample"))[0],
                client=client,
                evidence_dir=path,
            )
        return run_interaction_episode(
            "J01" if experiment == "E2-mechanism" else "ZH-TR-01", client, "A5", 1, path
        )

    recorder = LLMClient(mode="record", api_key="synthetic-test-credential", **options)
    original = episode(recorder, tmp_path / "original")
    assert recorder.api_calls == 1 and recorder.spent_usd > 0
    monkeypatch.setattr(
        httpx.HTTPTransport,
        "handle_request",
        lambda *args: pytest.fail("Strict replay attempted network access"),
    )
    replayer = LLMClient(mode="strict", **options)
    replayed = episode(replayer, tmp_path / "replayed")
    assert replayer.api_calls == 0 and replayer.spent_usd == 0
    assert original["metrics"]["model_calls"] == replayed["metrics"]["model_calls"] == 1
    if experiment == "E2-mechanism":
        assert original["metrics"]["input_tokens"] == 30
        assert original["metrics"]["output_tokens"] == 8
        assert "input_tokens" not in replayed["metrics"]
        assert replayer.calls[0]["recorded_usage"]["input_tokens"] == 30
    assert observation_hash(replay_projection(original)) == observation_hash(
        replay_projection(replayed)
    )


def test_fault_classes_respect_predeclared_stop_requirement():
    assert (
        fault_outcome("must_stop", completed=True, continued=True, harmed=False) == "wrong_continue"
    )
    assert (
        fault_outcome("must_resume", completed=True, continued=True, harmed=False)
        == "recovery_completed"
    )
    assert (
        fault_outcome("must_resume", completed=False, continued=False, harmed=False) == "safe_stop"
    )


def test_common_transfer_counts_real_effects_and_confirmation(tmp_path):
    result = run_interaction_episode("ZH-TR-01", LLMClient(mode="scripted"), "A5", 1, tmp_path)
    assert result["completed"] and result["status"] == "valid_completed"
    assert result["metrics"]["user_turns"] == 1
    assert result["metrics"]["confirmations"] == 1
    assert result["metrics"]["tool_calls"] >= 1


def test_unavailable_partial_configuration_does_not_run_production(tmp_path):
    client = LLMClient(mode="scripted")
    result = run_interaction_episode("ZH-BI-01", client, "A2", 1, tmp_path)
    assert result["status"] == "not_run" and not result["completed"]
    assert client.calls == []


def _observed_judge(task, client, root, *, repeat=False):
    from corridor.bench.bank_cases import BankCase
    from corridor.bench.investment_runner import _with_mfa
    from corridor.bench.planning_runner import prepare_transfer
    from corridor.sim.fixtures import make_state

    client.complete(
        task_id=task,
        step="observed",
        prompt={
            "response_schema": {
                "type": "object",
                "properties": {"ok": {"type": "boolean"}},
                "required": ["ok"],
                "additionalProperties": False,
            }
        },
        tool_state={},
        scripted=lambda: {"ok": True},
    )
    bank = BankCase(make_state(), task_id=task, subcase_id="partial")
    first = _with_mfa(bank, prepare_transfer(bank, "1200/1", step=0))
    assert bank.dispatch(first).status == "settled"
    if repeat:
        second = _with_mfa(bank, prepare_transfer(bank, "1200/1", step=1))
        bank.dispatch(second)
    raise RuntimeError("Synthetic apparatus failure after observed costs")


def test_judge_error_preserves_actual_calls_approval_mfa_and_bank_effects(tmp_path, monkeypatch):
    monkeypatch.setattr("corridor.cli.judge.run_bank_task", _observed_judge)
    result = run_interaction_episode("J01", LLMClient(), "A5", 1, tmp_path)
    assert result["status"] == "runtime_error"
    assert result["error_class"] == "apparatus_defect"
    assert result["metrics"]["model_calls"] == 1
    assert result["metrics"]["tool_calls"] == 1
    assert result["metrics"]["confirmations"] == 1
    assert result["metrics"]["mfa"] == 1
    assert "user_turns" not in result["metrics"]
    assert result["business_effects"][0]["balances"]["A_CURRENT"] == "8800/1"


def test_fixed_budget_truncation_is_measured_incomplete_not_apparatus_failure(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("corridor.bench.interaction.MAX_TOOL_CALLS", 1)
    monkeypatch.setattr(
        "corridor.cli.judge.run_bank_task", lambda *args: _observed_judge(*args, repeat=True)
    )
    result = run_interaction_episode("J01", LLMClient(), "A5", 1, tmp_path)
    assert result["status"] == "truncated" and not result["completed"]
    assert result["truncated"] is True
    assert result["termination"] == "budget_truncated"
    assert result["truncation_limit"] == "tool_calls"
    assert result["metrics"]["model_calls"] == 1
    assert result["metrics"]["tool_calls"] == 1


def test_chinese_failure_retains_started_turn_and_model_call(tmp_path, monkeypatch):
    from corridor.agent.domestic_session import DomesticSession

    def failed_receive(session, *args, **kwargs):
        session.client.complete(
            task_id="FAILURE",
            step="intent",
            prompt={},
            tool_state={},
            scripted=lambda: {"intent": "refuse"},
        )
        raise ValueError("Synthetic failure after model call")

    monkeypatch.setattr(DomesticSession, "receive", failed_receive)
    result = run_interaction_episode("ZH-BI-01", LLMClient(), "A5", 1, tmp_path)
    assert result["status"] == "runtime_error"
    assert result["metrics"]["user_turns"] == 1
    assert result["metrics"]["model_calls"] == 1
    assert result["metrics"]["tool_calls"] == 0
    assert "false_blocks_of_legal_actions" not in result["metrics"]


@pytest.mark.parametrize("resource", [None, "tool_calls", "budget"])
def test_interaction_replays_error_and_truncated_prefixes(tmp_path, monkeypatch, resource):
    import httpx

    from corridor.bench.live_zh import replay_projection
    from corridor.bench.recording import observation_hash

    def transport(self, request):
        return httpx.Response(
            200,
            json={
                "model": "gpt-6.1-sol",
                "status": "completed",
                "output": [
                    {"type": "function_call", "name": "emit_result", "arguments": '{"ok":true}'}
                ],
                "usage": {"input_tokens": 30, "output_tokens": 8, "total_tokens": 38},
            },
        )

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", transport)
    if resource == "tool_calls":
        monkeypatch.setattr("corridor.bench.interaction.MAX_TOOL_CALLS", 1)
    monkeypatch.setattr(
        "corridor.cli.judge.run_bank_task",
        lambda *args: _observed_judge(*args, repeat=resource == "tool_calls"),
    )
    options = dict(
        model="gpt-6.1-sol",
        api="responses",
        replay_dir=tmp_path / "replay",
        env_file=None,
        max_output_tokens=128,
    )
    recorder = LLMClient(mode="record", api_key="synthetic", max_calls=10, max_usd="10", **options)
    if resource == "budget":
        recorder.max_calls = 0
    original = run_interaction_episode("J01", recorder, "A5", 1, tmp_path / "original")
    assert original["status"] == ("truncated" if resource else "runtime_error")
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", lambda *args: pytest.fail("Network"))
    replayer = LLMClient(mode="strict", **options)
    replayed = run_interaction_episode(
        "J01", replayer, "A5", 1, tmp_path / "replayed", replay_observation=original
    )
    assert observation_hash(replay_projection(replayed)) == observation_hash(
        replay_projection(original)
    )
    assert replayer.api_calls == 0
