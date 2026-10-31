"""Pinned upstream pipeline construction; imported only in the isolated E4 env."""

from agentdojo.agent_pipeline.agent_pipeline import AgentPipeline, PipelineConfig
from agentdojo.agent_pipeline.base_pipeline_element import BasePipelineElement
from agentdojo.agent_pipeline.tool_execution import ToolsExecutor
from agentdojo.types import (
    ChatAssistantMessage,
    ChatToolResultMessage,
    text_content_block_from_string,
)
from agentdojo_calibrate import exact_wire

from corridor.bench.agentdojo_gate import NativeTransferGate


class GatedToolsExecutor(ToolsExecutor):
    """Check each call immediately before its actual upstream run_function."""

    def __init__(self, gate):
        super().__init__()
        self.gate = gate
        self.dispatch_events = []

    def query(self, query, runtime, env, messages=(), extra_args=None):
        if not messages or messages[-1]["role"] != "assistant":
            return query, runtime, env, messages, extra_args or {}
        results = []
        for call in messages[-1].get("tool_calls") or []:
            decision = self.gate.evaluate(call.function, exact_wire(call.args))
            if decision["allow"]:
                result, error = runtime.run_function(env, call.function, call.args)
                self.gate.record_external_result(decision, error)
                self.dispatch_events.append({
                    "function": call.function,
                    "argument_hash": decision["argument_hash"],
                })
                content = self.output_formatter(result)
            else:
                content, error = "", f"Corridor rejected: {decision['reason']}"
            results.append(
                ChatToolResultMessage(
                    role="tool",
                    content=[text_content_block_from_string(content)],
                    tool_call_id=call.id,
                    tool_call=call,
                    error=error,
                )
            )
        return query, runtime, env, [*messages, *results], extra_args or {}


class NonemptyTerminal(BasePipelineElement):
    """Avoid upstream replaying a mutated episode because assistant text is None."""

    def query(self, query, runtime, env, messages=(), extra_args=None):
        if not messages or messages[-1]["role"] != "assistant" or not messages[-1].get("content"):
            messages = [
                *messages,
                ChatAssistantMessage(
                    role="assistant",
                    content=[
                        text_content_block_from_string(
                            "Execution stopped; inspect tool results for operation status."
                        )
                    ],
                    tool_calls=None,
                ),
            ]
        return query, runtime, env, messages, extra_args or {}


def build_pipeline(llm, config, *, prompt, initial_environment, approval_mode="prompt-bound"):
    """Caller supplies the recorded bridge; unsupported A5 writes fail closed."""
    if not llm.name or not any(name in llm.name for name in ("local", "openai-compatible")):
        raise ValueError("Pipeline name must include local or openai-compatible")
    if config not in {"A0", "A5", "spotlighting_with_delimiting", "partial_native_transfer_gate"}:
        raise ValueError("Unknown E4 pipeline configuration")
    pipeline = AgentPipeline.from_config(
        PipelineConfig(
            llm=llm,
            model_id=None,
            defense="spotlighting_with_delimiting"
            if config == "spotlighting_with_delimiting"
            else None,
            system_message_name=None,
            system_message=None,
        )
    )
    loop = list(pipeline.elements)[-1]
    loop.max_iters = 16
    pipeline.gate = None
    if config in {"A5", "partial_native_transfer_gate"}:
        gate = NativeTransferGate(
            prompt, exact_wire(initial_environment.model_dump()), approval_mode=approval_mode
        )
        loop.elements = [GatedToolsExecutor(gate), llm]
        pipeline.gate = gate
    pipeline.elements = [*pipeline.elements, NonemptyTerminal()]
    pipeline.name = f"{llm.name}-{config}"
    return pipeline
