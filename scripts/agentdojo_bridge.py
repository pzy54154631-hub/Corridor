"""Native named-tool proposals through Corridor's single recording boundary."""

from agentdojo.agent_pipeline.base_pipeline_element import BasePipelineElement
from agentdojo.functions_runtime import FunctionCall
from agentdojo.types import ChatAssistantMessage, text_content_block_from_string
from agentdojo_calibrate import exact_wire

from corridor.agent.llm import LLMOutputError
from corridor.bench.agentdojo_contract import synthetic_context_guard as synthetic_context_guard


def wire_schema(schema):
    """Preserve upstream schema, spelling monetary numbers as exact strings."""
    if isinstance(schema, list):
        return [wire_schema(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    result = {key: wire_schema(value) for key, value in schema.items()}
    if result.get("type") == "number":
        result["type"] = "string"
    return result


def wire_messages(messages):
    return exact_wire([
        {
            key: [call.model_dump() for call in value]
            if key == "tool_calls" and value
            else value.model_dump()
            if key == "tool_call"
            else value
            for key, value in message.items()
        }
        for message in messages
    ])


class CorridorLLM(BasePipelineElement):
    name = "openai-compatible-corridor-recorded"

    def __init__(self, client, *, task_id, seed, scripted=None):
        if client.tool_namespace != "agentdojo-banking-v1.2.2":
            raise ValueError("External bridge requires the explicit benchmark tool namespace")
        self.client, self.task_id, self.seed = client, task_id, seed
        self.scripted = scripted
        self.steps = 0

    def query(self, query, runtime, env, messages=(), extra_args=None):
        self.steps += 1
        tools = [
            {
                "type": "function",
                "name": tool.name,
                "description": tool.description,
                "parameters": wire_schema(tool.parameters.model_json_schema()),
                "strict": True,
            }
            for tool in runtime.functions.values()
        ]
        tools.append({
            "type": "function",
            "name": "finish",
            "description": "End the task with a final user-facing message.",
            "parameters": {"type": "object", "properties": {"message": {"type": "string"}}},
            "strict": True,
        })

        def scripted():
            if self.scripted is None:
                if self.client.mode != "scripted":
                    raise TimeoutError("No model output; scripted fallback is prohibited")
                return {"tool": "finish", "arguments": {"message": "No tools were called."}}
            return self.scripted(self.steps, messages)

        result = self.client.complete(
            task_id=self.task_id,
            step=f"tool-{self.steps}",
            sample_idx=self.seed,
            prompt={
                "benchmark": "agentdojo-banking-v1.2.2",
                "query": query,
                "messages": wire_messages(messages),
                "tools": tools,
                "transport_note": "Use decimal strings for money; call finish when done.",
            },
            tool_state={},
            scripted=scripted,
        ).output
        name, arguments = result["tool"], result["arguments"]
        if name == "finish":
            if set(arguments) != {"message"} or not isinstance(arguments["message"], str):
                raise LLMOutputError("Invalid terminal message")
            content = [text_content_block_from_string(arguments["message"] or "Execution stopped.")]
            calls = None
        else:
            # Upstream validates its own typed arguments at run_function; failed
            # validation becomes a tool error and never broadens the native gate.
            content = None
            calls = [FunctionCall(function=name, args=arguments, id=f"corridor-call-{self.steps}")]
        message = ChatAssistantMessage(role="assistant", content=content, tool_calls=calls)
        return query, runtime, env, [*messages, message], extra_args or {}
