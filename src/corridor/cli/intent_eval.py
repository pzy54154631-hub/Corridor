"""Evaluate the frozen colloquial Chinese recognition set."""

from pathlib import Path

import typer

from corridor.agent.llm import LLMClient
from corridor.bench.interaction import run_intent_eval
from corridor.bench.live_credentials import named_env_options
from corridor.ui.controller import DemoController


def run(
    planner: str = typer.Option("scripted"),
    model: str = typer.Option("scripted-bank-v1"),
    output: Path = typer.Option(Path("evidence/dryrun/intents")),
    env_file: Path = typer.Option(
        Path(".env"), help="Read live credentials only from this UTF-8 file."
    ),
):
    controller = DemoController()
    client = LLMClient(
        mode="record" if planner == "live" else planner,
        model=model,
        replay_dir=output / "replay",
        context_guard=controller.gateway.vault.ensure_safe,
        **named_env_options(planner, model, env_file),
    )
    result = run_intent_eval(output=output, client=client)
    typer.echo(
        f"intent: {result['intent_correct']}/{result['total']}; "
        f"parameters: {result['parameter_exact_match']['accuracy']}; {output}"
    )
    for field, metric in result["parameter_accuracy"].items():
        present = metric["present_accuracy"] or metric["present_status"]
        typer.echo(f"{field}: {metric['accuracy']}; non-null: {present}")


def register(app):
    app.command("intent-eval")(run)
