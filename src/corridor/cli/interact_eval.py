"""Run the bounded domestic interaction apparatus."""

from pathlib import Path

import typer

from corridor.bench.interaction import run_interact_eval


def run(
    planner: str = typer.Option("scripted"),
    model: str = typer.Option("scripted-bank-v1"),
    config: str = typer.Option("A5"),
    cases: str = typer.Option(""),
    output: Path = typer.Option(Path("evidence/dryrun/interaction")),
    env_file: Path = typer.Option(
        Path(".env"), help="Read live credentials only from this UTF-8 file."
    ),
):
    result = run_interact_eval(
        output=output,
        planner=planner,
        model=model,
        config=config,
        cases=cases.split(",") if cases else None,
        env_file=env_file,
    )
    typer.echo(
        f"interaction: {result['summary']['valid_completed']}/{result['summary']['planned']}; "
        f"{output}"
    )


def register(app):
    app.command("interact-eval")(run)
