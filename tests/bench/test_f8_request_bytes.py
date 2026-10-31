"""No model requests: freeze-7 and freeze-8 bytes captured by a mock transport."""

import sys
import types

import httpx
import pytest

from corridor.agent import llm
from corridor.agent.scripted_planner import TransferProposal
from corridor.crypto.jcs import jcs_bytes
from corridor.schemas.base import loads_exact
from tests.frozen_source import frozen_source


@pytest.fixture
def frozen_llm(monkeypatch):
    source = frozen_source("domestic-freeze-7", "src/corridor/agent/llm.py")
    module = types.ModuleType("corridor.agent._freeze7_wire_test")
    monkeypatch.setitem(sys.modules, module.__name__, module)
    exec(compile(source, "freeze7_llm.py", "exec"), module.__dict__)
    return module


@pytest.mark.parametrize(
    "provider,api",
    [
        ("openai", "responses"),
        ("openai", "chat"),
        ("deepseek", "chat"),
        ("zhipu", "chat"),
        ("ollama", "chat"),
    ],
)
@pytest.mark.parametrize("shape", ["simple", "nested_transfer", "e4"])
def test_all_provider_request_bytes_match_freeze7(
    tmp_path, monkeypatch, frozen_llm, provider, api, shape
):
    for name in tuple(__import__("os").environ):
        if name.startswith("LLM_"):
            monkeypatch.delenv(name)
    bodies = []

    def send(_transport, request):
        bodies.append(request.content)
        body = loads_exact(request.content)
        arguments = jcs_bytes({"handle": "OK"}).decode("utf-8")
        name = "get_balance" if shape == "e4" else "emit_result"
        if api == "responses":
            response = {
                "id": "synthetic",
                "object": "response",
                "model": body["model"],
                "status": "completed",
                "output": [
                    {
                        "type": "function_call",
                        "name": name,
                        "call_id": "synthetic",
                        "arguments": arguments,
                    }
                ],
                "usage": {"input_tokens": 300, "output_tokens": 20, "total_tokens": 320},
            }
        else:
            response = {
                "id": "synthetic",
                "object": "chat.completion",
                "model": body["model"],
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "synthetic",
                                    "type": "function",
                                    "function": {"name": name, "arguments": arguments},
                                }
                            ],
                        },
                    }
                ],
                "usage": {"prompt_tokens": 300, "completion_tokens": 20, "total_tokens": 320},
            }
        return httpx.Response(200, json=response)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", send)
    schema = (
        TransferProposal.model_json_schema()
        if shape == "nested_transfer"
        else {
            "type": "object",
            "properties": {"handle": {"type": "string"}},
            "required": ["handle"],
            "additionalProperties": False,
        }
    )
    prompt = {"instruction": "Select a handle.", "response_schema": schema}
    if shape == "e4":
        prompt = {
            "tools": [
                {
                    "type": "function",
                    "name": "get_balance",
                    "description": "Return balance.",
                    "parameters": {
                        "type": "object",
                        "properties": {},
                        "required": [],
                        "additionalProperties": False,
                    },
                    "strict": True,
                }
            ]
        }
    bases = {
        "openai": "https://api.openai.com/v1",
        "deepseek": "https://api.deepseek.com/v1",
        "zhipu": "https://open.bigmodel.cn/api/paas/v4",
        "ollama": "http://127.0.0.1:11434/v1",
    }
    for index, module in enumerate((frozen_llm, llm)):
        client = module.LLMClient(
            mode="record",
            model="gpt-6.1-sol" if api == "responses" else "synthetic-model",
            replay_dir=tmp_path / str(index),
            provider=provider,
            api=api,
            api_base=bases[provider],
            api_key="SYNTHETIC",
            max_calls=1,
            max_usd="1",
            env_file=None,
            assumed_price_in="2",
            assumed_price_out="8",
            tool_namespace="agentdojo-banking-v1.2.2" if shape == "e4" else "wealth",
        )
        client.complete(
            task_id="F8-WIRE",
            step="fixed",
            prompt=prompt,
            tool_state={"handles": ["OK"]},
            scripted=lambda: {},
        )
    assert len(bodies) == 2
    assert bodies[0] == bodies[1]
