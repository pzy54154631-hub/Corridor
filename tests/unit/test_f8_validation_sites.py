"""Enumerate the memo's rejection sites and discovered model schema boundaries."""

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from corridor.agent.llm import LLMClient, LLMOutputError
from corridor.bench import (
    billing_runner,
    domestic_baselines,
    investment_runner,
    judge_runner,
    planning_runner,
    sandbox_runner,
    security_runner,
)
from corridor.bench.episode_control import error_class

# Each message identifies one distinct post-call validation boundary. The AST
# check prevents a future plain ValueError from silently changing its class.
SITES = {
    "agent/dialogue.py": [
        "Planner repair limit reached",
        "Planner attempted to change signed handles",
        "RCPT-MISMATCH: Narrative changed the requested receipt",
    ],
    "bench/blind_zh.py": [
        "Invalid typed banking proposal",
        "Invalid assistant narrative",
        "Selected role is incompatible with the typed action",
    ],
    "bench/judge_runner.py": ["Unsupported judge scenario proposal"],
    "bench/billing_runner.py": ["Planner selected an unsupported billing proposal"],
    "bench/planning_runner.py": ["Unsupported planning proposal"],
    "bench/investment_runner.py": ["Investment planner selected an unsupported operation"],
    "bench/security_runner.py": ["Unexpected security intent"],
    "bench/sandbox_runner.py": ["Planner must return one restricted analysis program"],
    "bench/domestic_baselines.py": ["Invalid LLM guard decision"],
    "agent/wealth_dialogue.py": ["Wealth planner repair limit reached"],
    "bench/nl_runner.py": ["NL planner repair limit reached"],
    "bench/notification_baseline.py": ["Notification proposal selected unknown handles"],
}


@pytest.mark.parametrize("filename,messages", SITES.items())
def test_enumerated_rejection_sites_raise_model_output_errors(filename, messages):
    root = Path(__file__).resolve().parents[2] / "src/corridor"
    tree = ast.parse((root / filename).read_text(encoding="utf-8"))
    for message in messages:
        matches = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Raise)
            and any(
                isinstance(child, ast.Constant) and child.value == message
                for child in ast.walk(node)
            )
        ]
        assert len(matches) == 1, (filename, message)
        call = matches[0].exc
        assert isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
        assert call.func.id in {"model_output_error", "LLMOutputError"}
    if filename == "bench/blind_zh.py":
        from corridor.bench.blind_zh import UnexpressibleProposal

        assert issubclass(UnexpressibleProposal, LLMOutputError)


@pytest.mark.parametrize(
    "site", ["judge", "billing", "planning", "investment", "security", "sandbox", "guard"]
)
def test_runner_contract_failures_are_model_failures(site, monkeypatch):
    client = LLMClient(mode="scripted", env_file=None)
    monkeypatch.setattr(
        client, "complete", lambda **kwargs: SimpleNamespace(output={"wrong": True})
    )
    case = SimpleNamespace(
        task_id="J01", subcase_id="synthetic", state={}, device=SimpleNamespace(principal="C_DEMO")
    )
    calls = {
        "judge": lambda: judge_runner.JudgeRun("J02", client, None).case("synthetic"),
        "billing": lambda: billing_runner._plan(client, case, "bill_report"),
        "planning": lambda: planning_runner._plan(client, case, "transfer"),
        "investment": lambda: investment_runner._plan(client, case, "invest_query"),
        "security": lambda: security_runner.run_security("J21", client=client),
        "sandbox": lambda: sandbox_runner._program(
            client, case, "Count rows", "return {}", result_keys=[]
        ),
        "guard": lambda: domestic_baselines.guard_action(
            client, task_id="test", step=0, legit_goal={}, candidate={}, trusted_facts={}
        ),
    }
    with pytest.raises(LLMOutputError) as caught:
        calls[site]()
    assert error_class(caught.value) == "model_output_format"


@pytest.mark.parametrize(
    "filename,schema",
    [
        ("agent/domestic_session.py", "IntentCandidate"),
        ("bench/notification_baseline.py", "TransferProposal"),
    ],
)
def test_discovered_direct_model_schema_checks_are_wrapped(filename, schema):
    root = Path(__file__).resolve().parents[2] / "src/corridor"
    tree = ast.parse((root / filename).read_text(encoding="utf-8"))
    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "validate_model_output"
        and any(isinstance(arg, ast.Name) and arg.id == schema for arg in node.args)
        for node in ast.walk(tree)
    )
