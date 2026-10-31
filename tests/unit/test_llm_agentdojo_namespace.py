"""External benchmark tools require explicit constructor-scoped capability."""

import pytest

from corridor.agent.llm import LLMClient, LLMConfigurationError
from corridor.bench.recording import replay_client


def _request(client, properties=None):
    return client.complete(
        task_id="e4-mock",
        step="1",
        tool_state={},
        prompt={
            "tools": [
                {
                    "type": "function",
                    "name": "finish",
                    "description": "End",
                    "strict": True,
                    "parameters": {
                        "type": "object",
                        "properties": properties or {"message": {"type": "string"}},
                    },
                }
            ]
        },
        scripted=lambda: {"tool": "finish", "arguments": {"message": "Done"}},
    )


def test_default_and_prompt_cannot_enable_external_tool_names():
    with pytest.raises(LLMConfigurationError):
        _request(LLMClient(mode="scripted"))
    with pytest.raises(LLMConfigurationError):
        LLMClient(mode="scripted", tool_namespace="arbitrary-tools")


def test_external_namespace_validates_exact_fields_types_and_records_identity():
    client = LLMClient(mode="scripted", tool_namespace="agentdojo-banking-v1.2.2")
    assert _request(client).output["tool"] == "finish"
    assert client.calls[-1]["tool_namespace"] == "agentdojo-banking-v1.2.2"
    with pytest.raises(LLMConfigurationError):
        _request(client, {"message": {"type": "number"}})
    with pytest.raises(LLMConfigurationError):
        _request(client, {"message": {"type": "string"}, "extra": {"type": "string"}})
    clone = replay_client(client)
    assert clone.tool_namespace == client.tool_namespace
    assert clone.context_guard == client.context_guard
