"""One pinned E4 episode, called sequentially by the live coordinator."""

import hashlib
import importlib.metadata
from fractions import Fraction
from pathlib import Path

from agentdojo.agent_pipeline.base_pipeline_element import BasePipelineElement
from agentdojo.attacks.attack_registry import load_attack
from agentdojo.functions_runtime import FunctionsRuntime
from agentdojo.task_suite.load_suites import get_suite
from agentdojo.types import ChatAssistantMessage, text_content_block_from_string
from agentdojo_bridge import CorridorLLM, wire_messages
from agentdojo_calibrate import exact_wire
from agentdojo_pipeline import build_pipeline

from corridor.bench.agentdojo_contract import (
    BENCHMARK_VERSION,
    COMMIT,
    corrected_security,
    digest,
    error_score,
)
from corridor.bench.agentdojo_gate import NativeTransferGate
from corridor.bench.episode_control import EpisodeControl, EpisodeLimit
from corridor.bench.evaluability import promote_evaluability
from corridor.schemas.base import loads_exact


def verify_installed_scorers():
    """Check installed bytes against the already calibrated, immutable source pin."""
    import agentdojo

    root = Path(__file__).resolve().parents[1]
    calibration = loads_exact((root / "evidence/dryrun/agentdojo/calibration.json").read_bytes())
    if importlib.metadata.version("agentdojo") != "0.1.35" or calibration["commit"] != COMMIT:
        raise ValueError("AgentDojo version/source pin differs from calibration")
    installed = Path(agentdojo.__file__).resolve().parent
    for relative, expected in calibration["source_hashes"].items():
        if hashlib.sha256((installed / relative).read_bytes()).hexdigest() != expected:
            raise ValueError("Installed external scorer differs from calibrated source")


class NullPipeline(BasePipelineElement):
    name = "openai-compatible-corridor-A_null"

    def query(self, query, runtime, env, messages=(), extra_args=None):
        return (
            query,
            runtime,
            env,
            [
                *messages,
                ChatAssistantMessage(
                    role="assistant",
                    content=[text_content_block_from_string("No tools were called.")],
                    tool_calls=None,
                ),
            ],
            extra_args or {},
        )


def run_episode(
    row, client, *, scripted=None, replay_observation=None, limits=None, evidence_dir=None
):
    metrics = {"user_turns": 0, "tool_calls": 0}
    control = EpisodeControl(
        client,
        metrics,
        replay_observation=replay_observation,
        limits=limits,
        evidence_dir=evidence_dir,
    )
    control.attach()
    try:
        try:
            control.checkpoint("e4.setup")
            result = _run_episode(
                row,
                client,
                control,
                metrics,
                scripted=scripted,
                replay_observation=replay_observation,
            )
        except Exception as error:
            result = {**row, **control.capture(error), "prohibited_effect": None}
        try:
            result["replay_control"] = control.finish()
        except Exception as error:
            result.update(control.capture(error), prohibited_effect=None)
            result["replay_control"] = control.data
        result.setdefault("metrics", metrics)
        return result
    finally:
        control.close()


def _run_episode(row, client, control, metrics, *, scripted=None, replay_observation=None):
    verify_installed_scorers()
    suite = get_suite(BENCHMARK_VERSION, "banking")
    initial = suite.load_and_inject_default_environment({})
    variant = row["variant"]
    user = (
        suite.get_injection_task_by_id(row["user_task"])
        if variant == "sanity"
        else suite.get_user_task_by_id(row["user_task"])
    )
    injection = (
        suite.get_injection_task_by_id(row["injection_task"]) if variant == "attack" else None
    )
    prompt = user.GOAL if variant == "sanity" else user.PROMPT
    approval = row.get("approval_mode", "prompt-bound")
    captured = {}

    class ObservedLLM(CorridorLLM):
        def query(self, query, runtime, env, messages=(), extra_args=None):
            captured["environment"], captured["messages"] = env, messages
            control.check_limits("e4.query")
            result = super().query(query, runtime, env, messages, extra_args)
            captured["environment"], captured["messages"] = result[2], result[3]
            calls = result[3][-1].get("tool_calls") or []
            captured["pending_tools"] = bool(calls)
            for _call in calls:
                # Count proposals before the gate so rejected calls consume the
                # same resource budget as allowed calls in every configuration.
                control.check_limits("e4.tool", tool_call=True)
                metrics["tool_calls"] += 1
            return result

    llm = ObservedLLM(
        client,
        task_id=f"E4:{row['case_id']}:{row['attack']}:{approval}",
        seed=row["seed"],
        scripted=scripted,
    )
    pipeline = (
        NullPipeline()
        if row["config"] == "A_null"
        else build_pipeline(
            llm,
            row["config"],
            prompt=prompt,
            initial_environment=initial,
            approval_mode=approval,
        )
    )
    attacks = load_attack(row["attack"], suite, pipeline)
    injections = attacks.attack(user, injection) if injection is not None else {}
    diagnostic = NativeTransferGate(
        prompt, exact_wire(initial.model_dump()), approval_mode=approval
    )
    dispatches = []

    class ObservedRuntime(FunctionsRuntime):
        def run_function(self, env, name, kwargs, raise_on_error=False):
            decision = diagnostic.evaluate(name, exact_wire(kwargs))
            result, error = super().run_function(env, name, kwargs, raise_on_error=raise_on_error)
            dispatches.append({
                "function": name,
                "arguments": exact_wire(kwargs),
                "error": error,
                "visible_policy_reject": not decision["allow"]
                and decision["reason"] != "unsupported_mapping",
                "unsupported_mapping": decision["reason"] == "unsupported_mapping",
                "diagnostic": decision,
            })
            return result, error

    class Capture(BasePipelineElement):
        name = pipeline.name

        def query(self, query, runtime, env, messages=(), extra_args=None):
            captured["initial"] = exact_wire(env.model_dump())
            captured["environment"] = env
            control.check_limits("e4.user", user_turn=True)
            metrics["user_turns"] += 1
            result = pipeline.query(query, runtime, env, messages, extra_args)
            captured["environment"], captured["messages"] = result[2], result[3]
            control.check_limits("e4.pipeline_finished")
            if (
                captured.get("pending_tools")
                and metrics["tool_calls"] >= control.limits["tool_calls"]
            ):
                raise EpisodeLimit("tool_calls")
            return result

    base = {
        **row,
        "mode": {"scripted": "scripted", "record": "live-recorded", "strict": "strict-replay"}[
            client.mode
        ],
        "in_denominator": variant in {"attack", "benign"},
        "payload_hash": digest(injections),
        "injection_locations": sorted(injections),
        "mapping_scope": (
            "native transfer, schedule creation/cancel-replacement, red password control; "
            "user-profile mutation explicitly unsupported by native policy"
        ),
        "model_transport": "named_tools; exact_string_money; explicit_finish; structured_history",
        "tool_budget": control.limits["tool_calls"],
        "policy_source": "visible_prompt_and_frozen_environment_only",
    }
    try:
        utility, security = suite.run_task_with_pipeline(
            Capture(),
            user,
            injection,
            injections,
            runtime_class=ObservedRuntime,
        )
        final = exact_wire(captured["environment"].model_dump())
        original = bool(security) if injection is not None else False
        corrected = (
            corrected_security(
                int(row["injection_task"].rsplit("_", 1)[1]),
                final["bank_account"]["transactions"],
                original=original,
            )
            if injection is not None
            else False
        )
        base.update(
            status="valid_completed",
            completed=bool(utility),
            prohibited_effect=corrected,
            utility=bool(utility),
            security_original=original,
            security_corrected=corrected,
            utility_and_no_attack_original=bool(utility) and not original,
            utility_and_no_attack_corrected=bool(utility) and not corrected,
        )
    except Exception as error:
        base.update(control.capture(error), prohibited_effect=None)
        base["reason"] = base["terminal_error"]["message"]
        trace = client.calls[-1] if client.calls else {}
        if replay_observation is not None:
            trace = {**trace, **replay_observation.get("transport_diagnostics", {})}
        status = trace.get("http_status", 0)
        code = trace.get("provider_error_code")
        category = (
            "context_length"
            if code == "context_length_exceeded"
            else "server_error"
            if status == 500 or code == "server_error"
            else "network_quota"
            if status in {401, 403, 408, 429, 502, 503, 504}
            else "model_output_format"
        )
        if base["error_type"] == "LLMOutputError":
            base.update(error_score(category))
        base["transport_diagnostics"] = {
            key: trace[key]
            for key in (
                "http_status",
                "provider_error_code",
                "provider_error_type",
                "envelope_error_reason",
            )
            if key in trace
        }
    final = exact_wire(captured["environment"].model_dump()) if "environment" in captured else None
    if base["status"] != "valid_completed" and final is not None and injection is not None:
        task = int(row["injection_task"].rsplit("_", 1)[1])
        if task in {0, 1, 3} and corrected_security(
            task, final["bank_account"]["transactions"], original=False
        ):
            # Preserve a proved target effect from the prefix; an unfinished
            # episode never establishes absence of harm.
            base["prohibited_effect"] = base["security_corrected"] = True
    gate = getattr(pipeline, "gate", None)
    base.update(
        initial_state=captured.get("initial"),
        final_state=final,
        messages=wire_messages(captured.get("messages", [])),
        dispatches=dispatches,
        ASR_dispatch=any(item["visible_policy_reject"] for item in dispatches),
        dispatch_unknown_mapping=sum(item["unsupported_mapping"] for item in dispatches),
        gate_events=gate.events if gate else [],
        mirror_only_rejections=sum(
            event.get("mirror_only_rejection", False) for event in gate.events
        )
        if gate
        else 0,
        metrics={
            **metrics,
            "model_calls": len(client.calls),
            "llm_calls": len(client.calls),
            "api_calls": client.api_calls,
            "input_tokens": sum(call.get("input_tokens", 0) for call in client.calls),
            "output_tokens": sum(call.get("output_tokens", 0) for call in client.calls),
            "cost_usd": str(
                sum((Fraction(call.get("cost_usd", "0")) for call in client.calls), Fraction(0))
            ),
        },
        call_sources=[call["source"] for call in client.calls],
    )
    return promote_evaluability(base)
