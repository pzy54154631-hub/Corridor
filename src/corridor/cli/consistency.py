"""Domestic E3 fixed-subset consistency apparatus."""

from pathlib import Path

import typer


def register(app: typer.Typer) -> None:
    @app.command("consistency")
    def consistency(
        planner: str = typer.Option("scripted"),
        model: str | None = typer.Option(None),
        config: str = typer.Option("A5"),
        output: Path = typer.Option(Path("evidence/dryrun/consistency")),
        env_file: Path = typer.Option(
            Path(".env"), help="Live credentials file; live runs use the gated coordinator."
        ),
    ) -> None:
        from corridor.bench.consistency import run_consistency

        result = run_consistency(
            output, planner=planner, model=model, config=config, env_file=env_file
        )
        typer.echo(str(result["summary"]))
