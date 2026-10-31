"""Probe the declared model deployments before formal experiments."""

from pathlib import Path

import typer

from corridor.bench.request_probe import probe_requests


def run(
    provider: str = typer.Option(...),
    output: Path = typer.Option(Path("evidence/probe/requests.json")),
    env_file: Path = typer.Option(
        Path(".env"),
        help="OpenAI credential file only; requires provider and endpoint declarations.",
    ),
    provider_env: list[str] = typer.Option(
        [], help="Provider-bound UTF-8 file: provider=path (repeatable)."
    ),
    tool_choice_mode: str = typer.Option(
        "forced_function", help="Domestic tool probe mode: forced_function or auto."
    ),
) -> None:
    bindings = {}
    for entry in provider_env:
        name, separator, path = entry.partition("=")
        if not separator or name not in {"deepseek", "zhipu"} or not path or name in bindings:
            raise typer.BadParameter("Use each domestic provider once as provider=path.")
        bindings[name] = Path(path)
    if provider in {"deepseek", "zhipu"}:
        if provider not in bindings:
            raise typer.BadParameter("Domestic probes require --provider-env provider=path.")
        selected_file = bindings[provider]
    else:
        selected_file = env_file
    try:
        options = (
            {"tool_choice_mode": tool_choice_mode} if provider in {"deepseek", "zhipu"} else {}
        )
        result = probe_requests(provider, output=output, env_file=selected_file, **options)
    except Exception as exc:
        # Typer's exception renderer must never inspect credential-bearing locals.
        typer.echo(f"{provider}: probe_error={type(exc).__name__}")
        raise typer.Exit(1) from None
    typer.echo(f"{provider}: gate_passed={result['gate_passed']}; evidence={output}")
    if not result["gate_passed"]:
        raise typer.Exit(1)


def register(app: typer.Typer) -> None:
    app.command("probe-requests")(run)
