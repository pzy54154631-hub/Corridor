"""Run the preregistered domestic witness gate without model calls."""

from pathlib import Path

import typer

from corridor.bench.witness import run_witness


def run(output: Path = typer.Option(Path("evidence/domestic/d3/witness"))) -> None:
    result = run_witness(output)
    passed = sum(row["passed"] for row in result["witnesses"])
    typer.echo(
        f"Witnesses: {passed}/{len(result['witnesses'])}; "
        f"C1 existing: {len(result['c1_existing'])}; additions: {len(result['c1_additions'])}"
    )
    coverage = result["operation_coverage"]
    typer.echo(
        f"Operation cells: {sum(item['evaluated'] for item in coverage.values())} evaluated; "
        f"{sum(item['not_evaluable'] for item in coverage.values())} not_evaluable."
    )
    typer.echo(
        "Scope: bank writes, schedule/AA creation and compensation; no future-tick equivalence."
    )
    typer.echo(f"Evidence: {output / 'witness.json'}")
    if not result["passed"]:
        raise typer.Exit(1)


def register(app: typer.Typer) -> None:
    app.command("witness")(run)
