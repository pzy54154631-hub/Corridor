"""Pinned E4 runner limits and replay tested without optional packages or providers."""

import importlib.util
import sys
from copy import deepcopy
from pathlib import Path
from types import ModuleType, SimpleNamespace

import httpx
import pytest

from corridor.agent.llm import LLMClient
from corridor.bench.live_zh import replay_projection
from corridor.bench.recording import observation_hash


@pytest.fixture
def e4_runner(monkeypatch):
    class Environment:
        def __init__(self):
            self.state = {"bank_account": {"transactions": []}}

        def model_dump(self):
            return deepcopy(self.state)

    class Runtime:
        def run_function(self, env, name, kwargs, raise_on_error=False):
            env.state["bank_account"]["transactions"].append({"tool": name})
            return "ok", None

    class Bridge:
        name = "openai-compatible-mock"

        def __init__(self, client, *, task_id, seed, scripted=None):
            self.client, self.task_id, self.seed = client, task_id, seed
            self.steps = 0

        def query(self, query, runtime, env, messages=(), extra_args=None):
            self.steps += 1
            self.client.complete(
                task_id=self.task_id,
                step=f"tool-{self.steps}",
                sample_idx=self.seed,
                prompt={
                    "query": query,
                    "response_schema": {
                        "type": "object",
                        "properties": {"ok": {"type": "boolean"}},
                        "required": ["ok"],
                        "additionalProperties": False,
                    },
                },
                tool_state={},
                scripted=lambda: {"ok": True},
            )
            call = SimpleNamespace(function="get_balance", args={})
            return query, runtime, env, [*messages, {"role": "assistant", "tool_calls": [call]}], {}

    class Pipeline:
        name = "openai-compatible-mock-pipeline"
        gate = None

        def __init__(self, llm):
            self.llm = llm

        def query(self, query, runtime, env, messages=(), extra_args=None):
            for _ in range(20):
                result = self.llm.query(query, runtime, env, messages, extra_args)
                messages = result[3]
                runtime.run_function(env, "get_balance", {})
            return result

    class Suite:
        def load_and_inject_default_environment(self, _):
            return Environment()

        def get_user_task_by_id(self, _):
            return SimpleNamespace(PROMPT="Inspect the synthetic balance")

        def run_task_with_pipeline(self, pipeline, user, injection, injections, runtime_class):
            pipeline.query(user.PROMPT, runtime_class(), Environment())
            return False, False

    def wire_messages(messages):
        return [
            {"role": item["role"], "tools": len(item.get("tool_calls") or [])} for item in messages
        ]

    definitions = {
        "agentdojo.agent_pipeline.base_pipeline_element": {"BasePipelineElement": object},
        "agentdojo.attacks.attack_registry": {"load_attack": lambda *args: None},
        "agentdojo.functions_runtime": {"FunctionsRuntime": Runtime},
        "agentdojo.task_suite.load_suites": {"get_suite": lambda *args: Suite()},
        "agentdojo.types": {"ChatAssistantMessage": dict, "text_content_block_from_string": str},
        "agentdojo_bridge": {"CorridorLLM": Bridge, "wire_messages": wire_messages},
        "agentdojo_calibrate": {"exact_wire": deepcopy},
        "agentdojo_pipeline": {"build_pipeline": lambda llm, *args, **kwargs: Pipeline(llm)},
    }
    for name, values in definitions.items():
        module = ModuleType(name)
        module.__dict__.update(values)
        monkeypatch.setitem(sys.modules, name, module)
    path = Path(__file__).resolve().parents[2] / "scripts/agentdojo_runner.py"
    spec = importlib.util.spec_from_file_location("e4_fix_l2_runner", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "verify_installed_scorers", lambda: None)
    monkeypatch.setattr(
        module,
        "NativeTransferGate",
        lambda *args, **kwargs: SimpleNamespace(
            evaluate=lambda *args: {"allow": True, "reason": "accepted"}
        ),
    )
    return module


@pytest.mark.parametrize("config", ["A0", "A5", "spotlighting_with_delimiting"])
def test_e4_tool_limit_replays_partial_effects_without_network(
    e4_runner, tmp_path, monkeypatch, config
):
    def transport(self, request):
        return httpx.Response(
            200,
            json={
                "model": "gpt-6.1-sol-mock",
                "status": "completed",
                "output": [
                    {"type": "function_call", "name": "emit_result", "arguments": '{"ok":true}'}
                ],
                "usage": {"input_tokens": 30, "output_tokens": 8, "total_tokens": 38},
            },
        )

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", transport)
    options = dict(
        model="gpt-6.1-sol",
        api="responses",
        replay_dir=tmp_path / "recording",
        env_file=None,
        max_calls=30,
        max_usd="10",
        max_output_tokens=128,
    )
    row = dict(
        case_id="synthetic-e4",
        variant="benign",
        user_task="user_task_0",
        attack="none",
        seed=1,
        config=config,
    )
    recorded = e4_runner.run_episode(
        row,
        LLMClient(mode="record", api_key="synthetic-test-credential", **options),
        limits={"user_turns": 8, "tool_calls": 2, "seconds": 600},
    )
    assert recorded["status"] == "truncated", recorded.get("terminal_error")
    assert recorded["metrics"]["tool_calls"] == len(recorded["dispatches"]) == 2
    assert len(recorded["final_state"]["bank_account"]["transactions"]) == 2
    monkeypatch.setattr(
        httpx.HTTPTransport, "handle_request", lambda *args: pytest.fail("Network in replay")
    )
    replayed = e4_runner.run_episode(
        row,
        LLMClient(mode="strict", **options),
        replay_observation=recorded,
        limits={"user_turns": 8, "tool_calls": 2, "seconds": 600},
    )
    assert replayed["status"] == "truncated"
    assert observation_hash(replay_projection(recorded)) == observation_hash(
        replay_projection(replayed)
    )


def test_e4_local_budget_stop_is_not_retryable_and_replays(e4_runner, tmp_path, monkeypatch):
    options = dict(
        model="gpt-6.1-sol",
        api="responses",
        replay_dir=tmp_path / "recording",
        env_file=None,
        max_calls=10,
        max_usd="1/1000000000",
        max_output_tokens=128,
    )
    monkeypatch.setattr(
        httpx.HTTPTransport, "handle_request", lambda *args: pytest.fail("Local budget made HTTP")
    )
    row = dict(
        case_id="synthetic-budget",
        variant="benign",
        user_task="user_task_0",
        attack="none",
        seed=1,
        config="A0",
    )
    recorder = LLMClient(mode="record", api_key="synthetic-test-credential", **options)
    recorded = e4_runner.run_episode(row, recorder)
    assert recorded["status"] == "truncated"
    assert recorded["error_class"] == "local_resource"
    assert recorded["metrics"]["tool_calls"] == recorder.api_calls == 0
    replayed = e4_runner.run_episode(
        row, LLMClient(mode="strict", **options), replay_observation=recorded
    )
    assert observation_hash(replay_projection(recorded)) == observation_hash(
        replay_projection(replayed)
    )


def test_e4_apparatus_error_replays_at_setup_checkpoint(e4_runner, tmp_path, monkeypatch):
    def broken_setup():
        raise ValueError("synthetic apparatus failure")

    options = dict(model="gpt-6.1-sol", replay_dir=tmp_path / "recording", env_file=None)
    row = dict(
        case_id="synthetic-broken",
        variant="benign",
        user_task="user_task_0",
        attack="none",
        seed=1,
        config="A0",
    )
    monkeypatch.setattr(e4_runner, "verify_installed_scorers", broken_setup)
    recorded = e4_runner.run_episode(row, LLMClient(mode="scripted", **options))
    assert recorded["status"] == "runtime_error"
    assert recorded["error_class"] == "apparatus_defect"
    monkeypatch.setattr(
        e4_runner, "verify_installed_scorers", lambda: pytest.fail("Skipped recorded stop")
    )
    replayed = e4_runner.run_episode(
        row, LLMClient(mode="strict", **options), replay_observation=recorded
    )
    assert recorded == replayed
