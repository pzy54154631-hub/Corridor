"""Chinese intent metrics and local structured evaluation entrypoints."""

from pathlib import Path

import typer

from corridor.agent.llm import LLMClient
from corridor.bench.domestic_runner import evaluate_intents, run_task
from corridor.crypto.jcs import jcs_bytes
from corridor.schemas.base import loads_exact


def task(file: Path = typer.Option(...), out: Path = typer.Option(...)):
    try:
        result = run_task(loads_exact(file.read_bytes()))
    except (ValueError, TypeError, KeyError, OSError):
        typer.echo("Invalid synthetic task input.", err=True)
        raise typer.Exit(2) from None
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(jcs_bytes(result))
    typer.echo(f"{result['final_status']}; planner=scripted; synthetic=true; out={out}")


def intent_eval(
    planner: str = "scripted",
    model: str = "scripted-bank-v1",
    replay_dir: Path = Path("data/replay/domestic"),
    source: Path | None = None,
    output: Path = Path("evidence/domestic/intent-eval.json"),
):
    if planner not in {"scripted", "strict", "live", "record"}:
        raise typer.BadParameter("Use scripted, strict, live or record")
    client = LLMClient(mode=planner, model=model, replay_dir=replay_dir, env_file=None)
    result = evaluate_intents(client=client, source=source)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(jcs_bytes(result))
    typer.echo(
        "Model | Planner | Recognition | Clarification | Parameters\n"
        f"{client.model} | {planner} | {result['intent_correct']}/{result['total']} | "
        f"{result['clarification_correct']}/{result['total']} | "
        f"{result['parameter_exact_match']['accuracy']}"
    )
    for field, metric in result["parameter_accuracy"].items():
        present = metric["present_accuracy"] or metric["present_status"]
        typer.echo(f"{field}: {metric['accuracy']}; non-null: {present}")
    if planner == "scripted":
        typer.echo("Scripted results validate the pipeline only; live quality: not_run.")


def serve(port: int = 8765):
    from corridor.channels.http import make_server

    server = make_server(port)
    typer.echo(f"Synthetic banking JSON API: http://127.0.0.1:{server.server_port}/run-task")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def register(app: typer.Typer):
    app.command("run-task")(task)
    app.command("intent-eval")(intent_eval)
    app.command("serve")(serve)
