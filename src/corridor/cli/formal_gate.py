"""Report every domestic formal prerequisite without running experiments."""

from pathlib import Path

import typer

from corridor.bench.formal_gate import LOCK, ROOT, run_formal_gate
from corridor.crypto.jcs import jcs_bytes


def run(
    root: Path = typer.Option(ROOT),
    witness: Path | None = typer.Option(None),
    probe: Path | None = typer.Option(None),
    lock: Path = typer.Option(LOCK),
    cases: Path | None = typer.Option(None),
    frozen_manifest: Path | None = typer.Option(None),
    e4: Path | None = typer.Option(None),
    output: Path | None = typer.Option(None),
    no_blind_access: bool = typer.Option(False, help="Skip blind checks and retain NOT READY."),
    offline_model_lock: bool = typer.Option(
        False, help="Verify locked files and saved probe without contacting Ollama."
    ),
) -> None:
    result = run_formal_gate(
        root=root,
        witness=witness,
        probe=probe,
        lock=lock,
        cases=cases,
        frozen_manifest=frozen_manifest,
        e4=e4,
        blind_access=not no_blind_access,
        offline_model_lock=offline_model_lock,
    )
    typer.echo(result["status"])
    for row in result["checks"]:
        typer.echo(f"{row['name']}: {'READY' if row['ready'] else 'NOT READY'}; {row['reason']}")
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(jcs_bytes(result))
    if not result["ready"]:
        raise typer.Exit(1)


def register(app: typer.Typer) -> None:
    app.command("formal-gate")(run)
