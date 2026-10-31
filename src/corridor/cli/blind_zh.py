"""Chinese blind evaluation with explicit recording and offline replay profiles."""

from contextlib import nullcontext
from pathlib import Path

import typer

from corridor.agent.llm import LLMClient, LLMConfigurationError, _environment
from corridor.bench.bank_cases import write_json
from corridor.bench.blind_zh import run_blind_zh, validate_cases
from corridor.bench.recording import network_disabled
from corridor.bench.request_probe import dotenv_credential
from corridor.schemas.base import loads_exact

CONFIGS = {"A0", "A1", "A2", "A3", "A4", "A5", "B_prompt", "B_guard"}
LOCAL_ALIASES = {
    "qwen3.8-27b": "qwen3.8:27b-",
    "qwen3-8b": "qwen3:8b-",
    "qwen3.5-4b": "qwen3.5:4b-",
}


def create_client(
    *,
    planner,
    model,
    config,
    seed,
    user_policy,
    replay_dir,
    max_calls,
    max_usd,
    replay="",
    env_file=Path(".env"),
):
    if planner not in {"scripted", "live"} or replay not in {"", "strict"}:
        raise LLMConfigurationError("Use --planner scripted|live and optional --replay strict")
    if config not in CONFIGS:
        raise LLMConfigurationError("Unknown domestic experiment configuration")
    if replay == "strict":
        profile = loads_exact((replay_dir / "client-profile.json").read_bytes())
        if profile["config"] != config or profile["user_policy"] != user_policy:
            raise LLMConfigurationError("Strict replay configuration or user policy differs")
        if profile["deployment_profile"]["repeat_seed"] != seed:
            raise LLMConfigurationError("Strict replay repeat identity differs")
        if model and model != profile["model"]:
            raise LLMConfigurationError("Strict replay model differs")
        return LLMClient(mode="strict", replay_dir=replay_dir, env_file=None, **profile)
    if planner == "scripted":
        return LLMClient(
            mode="scripted",
            model=model or "scripted-bank-v1",
            config=config,
            user_policy=user_policy,
            replay_dir=replay_dir,
            env_file=None,
        )
    env = _environment(env_file)
    selected_model = model or env.get("LLM_MODEL", "")
    provider = env.get("LLM_PROVIDER", "openai")
    base = env.get("LLM_API_BASE", "https://api.openai.com/v1")
    local = base.rstrip("/") == "http://127.0.0.1:11434/v1"
    if local:
        from corridor.bench.request_probe import local_deployments

        profiles = local_deployments()
        prefix = LOCAL_ALIASES.get(selected_model)
        matches = [
            profile
            for profile in profiles
            if profile["model"] == selected_model
            or (prefix and profile["model"].startswith(prefix))
        ]
        if len(matches) != 1:
            raise LLMConfigurationError("Local model must match exactly one verified deployment")
        deployment = {**matches[0], "repeat_seed": seed}
        selected_model, provider = deployment["model"], "ollama"
        api = "chat"
    else:
        api = env.get("LLM_API") or (
            "responses" if selected_model.removeprefix("openai/").startswith("gpt-6") else "chat"
        )
        deployment = {
            "model": selected_model,
            "repeat_seed": seed,
            "context_window": "provider-managed",
            "temperature": "provider-default",
            "reasoning_effort": "low" if api == "responses" else "none",
            "max_output_tokens": 512,
            "tool_budget": 16,
        }
    client = LLMClient(
        mode="record",
        model=selected_model,
        config=config,
        user_policy=user_policy,
        replay_dir=replay_dir,
        env_file=env_file,
        provider=provider,
        api_base=base,
        api=api,
        seed=seed if api == "chat" else None,
        reasoning_effort="none" if local else deployment["reasoning_effort"],
        max_calls=max_calls,
        max_usd=max_usd,
        deployment_profile=deployment,
        request_timeout_seconds=120 if local else 20,
        api_key="ollama" if local else dotenv_credential(env_file),
        assumed_price_in="1" if local else None,
        assumed_price_out="1" if local else None,
    )
    # The replay profile contains configuration only. Never serialize _api_key or dotenv.
    profile = {
        name: getattr(client, name)
        for name in (
            "model",
            "config",
            "user_policy",
            "api",
            "api_base",
            "provider",
            "local_mock",
            "reasoning_effort",
            "seed",
            "temperature",
            "deployment_profile",
            "max_output_tokens",
        )
    }
    profile_path = replay_dir / "client-profile.json"
    if profile_path.exists() and loads_exact(profile_path.read_bytes()) != profile:
        raise LLMConfigurationError("Refusing to replace a different frozen replay profile")
    write_json(profile_path, profile)
    return client


def run(
    planner: str = typer.Option("scripted"),
    model: str = typer.Option(""),
    config: str = typer.Option("A5"),
    cases: Path = typer.Option(Path("data/blind-zh")),
    output: Path = typer.Option(Path("evidence/blind-zh")),
    seed: int = typer.Option(1, min=1),
    user_policy: str = typer.Option("goal_bound"),
    replay_dir: Path | None = typer.Option(None),
    replay: str = typer.Option(""),
    max_calls: int | None = typer.Option(None),
    max_usd: str | None = typer.Option(None),
    case_id: str | None = typer.Option(None),
    variant: str | None = typer.Option(None),
    env_file: Path = typer.Option(
        Path(".env"), help="Read live credentials only from this UTF-8 file."
    ),
    validate: bool = typer.Option(
        False, "--validate", help="Strictly validate files without running."
    ),
) -> None:
    try:
        if validate:
            checks = validate_cases(cases)
            for check in checks:
                typer.echo(f"{check['file_id']}: {'PASS' if check['passed'] else 'FAIL'}")
            if not checks or not all(check["passed"] for check in checks):
                raise typer.Exit(1)
            return
        if variant not in {None, "attack", "legit_twin"}:
            raise LLMConfigurationError("Variant must be attack or legit_twin")
        client = create_client(
            planner=planner,
            model=model,
            config=config,
            seed=seed,
            user_policy=user_policy,
            replay_dir=replay_dir or output / "replay",
            max_calls=max_calls,
            max_usd=max_usd,
            replay=replay,
            env_file=env_file,
        )
        with network_disabled() if replay == "strict" else nullcontext():
            result = run_blind_zh(
                cases,
                output,
                planner=planner,
                config=config,
                client=client,
                seed=seed,
                user_policy=user_policy,
                case_id=case_id,
                variant=variant,
            )
        typer.echo(
            f"blind-zh: planned={result['planned']} valid_completed={result['valid_completed']} "
            f"runtime_errors={result['runtime_errors']} not_run={result['not_run']} "
            f"invalid_case={result['invalid_case']} "
            f"unsupported_oracle={result['unsupported_oracle']}; evidence={output}"
        )
        if result["runtime_errors"] or result["invalid_case"]:
            raise typer.Exit(1)
    except typer.Exit:
        raise
    except (ValueError, RuntimeError, OSError) as exc:
        typer.echo(f"blind-zh refused: {type(exc).__name__}", err=True)
        raise typer.Exit(2) from None


def register(app: typer.Typer) -> None:
    app.command("blind-zh")(run)
