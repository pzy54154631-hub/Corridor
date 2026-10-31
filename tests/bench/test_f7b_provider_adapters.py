"""F7b transport adaptation, environment isolation and freeze-6 compatibility."""

import sys
import types

import httpx
import pytest

from corridor.agent import llm
from corridor.bench import request_probe
from corridor.crypto.jcs import jcs_bytes
from corridor.schemas.base import loads_exact
from tests.frozen_source import frozen_source


def _client(module, tmp_path, provider, api="chat", profile=None):
    return module.LLMClient(
        mode="record",
        model="gpt-6.1-sol" if api == "responses" else "synthetic-model",
        config="F7b-wire",
        replay_dir=tmp_path,
        provider=provider,
        api=api,
        api_base="https://api.openai.com/v1"
        if provider == "openai"
        else "http://127.0.0.1:11434/v1"
        if provider == "ollama"
        else "https://api.deepseek.com/v1"
        if provider == "deepseek"
        else "https://open.bigmodel.cn/api/paas/v4",
        api_key=f"SYNTHETIC-{provider}",
        max_calls=1,
        max_usd="1",
        env_file=None,
        assumed_price_in="2",
        assumed_price_out="8",
        deployment_profile=profile,
    )


def _call(client):
    return client.complete(
        task_id="F7B-WIRE",
        step="fixed",
        prompt={
            "instruction": "Select a handle.",
            "response_schema": {
                "type": "object",
                "properties": {"handle": {"type": "string", "enum": ["OK"]}},
                "required": ["handle"],
                "additionalProperties": False,
            },
        },
        tool_state={"handles": ["OK"]},
        scripted=lambda: {"handle": "OK"},
    )


def _reply(api="chat", *, tokens=20, reasoning=0, finish="tool_calls"):
    if api == "responses":
        return {
            "id": "synthetic",
            "object": "response",
            "model": "gpt-6.1-sol",
            "status": "completed",
            "output": [
                {
                    "type": "function_call",
                    "name": "emit_result",
                    "call_id": "synthetic",
                    "arguments": '{"handle":"OK"}',
                }
            ],
            "usage": {"input_tokens": 300, "output_tokens": tokens, "total_tokens": 300 + tokens},
        }
    return {
        "id": "synthetic",
        "object": "chat.completion",
        "model": "synthetic-model",
        "choices": [
            {
                "index": 0,
                "finish_reason": finish,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "synthetic",
                            "type": "function",
                            "function": {"name": "emit_result", "arguments": '{"handle":"OK"}'},
                        }
                    ],
                },
            }
        ],
        "usage": {
            "prompt_tokens": 300,
            "completion_tokens": tokens,
            "total_tokens": 300 + tokens,
            "completion_tokens_details": {"reasoning_tokens": reasoning},
        },
    }


@pytest.mark.parametrize("provider", ["deepseek", "zhipu"])
@pytest.mark.parametrize("mode", ["forced_function", "auto"])
def test_domestic_wire_disables_thinking_and_scrubs_inherited_headers(
    tmp_path, monkeypatch, provider, mode
):
    monkeypatch.setenv("OPENAI_CUSTOM_HEADERS", "Authorization: Bearer WRONG\nOpenAI-Test: WRONG")
    monkeypatch.setenv("OPENAI_ORG_ID", "WRONG")
    monkeypatch.setenv("OPENAI_PROJECT_ID", "WRONG")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "WRONG")
    monkeypatch.setenv("ZHIPU_API_KEY", "WRONG")
    monkeypatch.setenv("LLM_API_KEY", "WRONG")
    seen = []

    def send(_self, request):
        seen.append(loads_exact(request.content))
        assert request.headers["authorization"] == f"Bearer SYNTHETIC-{provider}"
        assert not any(name.lower().startswith("openai-") for name in request.headers)
        return httpx.Response(200, json=_reply())

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", send)
    with request_probe.domestic_environment():
        client = _client(
            llm, tmp_path, provider, profile={"thinking": "disabled", "tool_choice_mode": mode}
        )
        _call(client)
    assert len(seen) == 1
    assert seen[0]["thinking"] == {"type": "disabled"}
    assert seen[0]["parallel_tool_calls"] is False
    assert seen[0]["tool_choice"] == (
        "auto" if mode == "auto" else {"type": "function", "function": {"name": "emit_result"}}
    )
    assert request_probe.os.environ["OPENAI_ORG_ID"] == "WRONG"


@pytest.mark.parametrize(
    "provider,api", [("openai", "responses"), ("openai", "chat"), ("ollama", "chat")]
)
def test_freeze6_nondomestic_request_bytes_are_unchanged(tmp_path, monkeypatch, provider, api):
    source = frozen_source("domestic-freeze-6", "src/corridor/agent/llm.py")
    baseline = types.ModuleType("corridor.agent._freeze6_test")
    monkeypatch.setitem(sys.modules, baseline.__name__, baseline)
    exec(compile(source, "freeze6_llm.py", "exec"), baseline.__dict__)
    bodies = []

    def send(_self, request):
        bodies.append(request.content)
        return httpx.Response(200, json=_reply(api))

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", send)
    for index, module in enumerate((baseline, llm)):
        _call(_client(module, tmp_path / str(index), provider, api))
    assert bodies[0] == bodies[1]


def test_formal_probe_uses_unchanged_development_prompt():
    request = request_probe.formal_probe_request()
    assert request["task_id"].startswith("sample-")
    assert "response_schema" in request["prompt"]
    assert request["tool_state"]["LEGIT_GOAL"]["kind"] == "transfer"
    assert len(jcs_bytes(request["prompt"])) > 5000


def test_environment_filter_removes_vendor_credentials_and_preserves_operating_settings():
    source = {
        "PATH": "synthetic-path",
        "OPENAI_CUSTOM_HEADERS": "bad",
        "LLM_API_KEY": "bad",
        "ANTHROPIC_API_KEY": "bad",
        "AZURE_OPENAI_API_KEY": "bad",
        "HOME": "home",
        "DEEPSEEK_API_KEY": "bad",
        "ZHIPU_API_KEY": "bad",
        "DASHSCOPE_API_KEY": "bad",
    }
    assert request_probe.domestic_worker_environment(source) == {
        "PATH": "synthetic-path",
        "HOME": "home",
    }


@pytest.mark.parametrize("declared", ["", "wrong-model"])
def test_domestic_credential_requires_exact_model_binding(tmp_path, declared):
    path = tmp_path / "synthetic.env"
    path.write_text(
        "LLM_PROVIDER=deepseek\nLLM_API_BASE=https://api.deepseek.com/v1\n"
        f"LLM_MODEL={declared}\nLLM_API_KEY=SYNTHETIC\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="credential_model_"):
        request_probe.load_provider_credentials(
            path,
            provider="deepseek",
            api_base="https://api.deepseek.com/v1",
            model="deepseek-flash",
        )
