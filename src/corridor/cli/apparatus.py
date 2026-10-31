"""Content-free apparatus validation commands."""

from pathlib import Path

import typer

from corridor.bench.apparatus import run_blackbox, run_sweep


def register(app):
    @app.command("apparatus-sweep")
    def sweep(output: Path = Path("evidence/apparatus-sweep")):
        result = run_sweep(output)
        typer.echo(
            f"apparatus-sweep: apparatus_defects={result['apparatus_defects']}; "
            f"model_output_formats={result['model_output_formats']}"
        )
        raise typer.Exit(0 if result["passed"] else 1)

    @app.command("apparatus-dryrun")
    def dryrun(
        cases: Path = Path("data/blind-zh/cases"),
        output: Path = Path("evidence/apparatus-dryrun"),
        workers: int = 1,
    ):
        allowed = (Path.cwd() / "evidence/apparatus-dryrun").resolve()
        if not output.resolve().is_relative_to(allowed):
            raise typer.BadParameter("Black-box output must stay under evidence/apparatus-dryrun")
        result = run_blackbox(cases, output, workers=workers)
        typer.echo(
            f"apparatus-dryrun: apparatus_defects={result['apparatus_defects']}; "
            f"model_output_formats={result['model_output_formats']}"
        )
        raise typer.Exit(0 if result["passed"] else 1)

    @app.command("exact-goal-witness")
    def exact_goal(
        cases: Path = Path("data/blind-zh/cases"),
        output: Path = Path("evidence/exact-goal-witness/summary.json"),
        workers: int = 1,
    ):
        from corridor.bench.exact_goal_witness import run_exact_goal_witness

        result = run_exact_goal_witness(cases, output, workers=workers)
        typer.echo(
            f"exact-goal-witness: passed={result['passed']}; counts={result['counts']}; "
            f"failures={result['failures']}"
        )
        raise typer.Exit(0 if result["passed"] else 1)
