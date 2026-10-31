"""Runtime-only named credential files for standalone domestic evaluators."""

from pathlib import Path

from corridor.agent.llm import _environment
from corridor.bench.request_probe import dotenv_credential


def named_env_options(planner: str, model: str, env_file: Path) -> dict:
    """Keep the selected key authoritative without changing recording identities."""
    if planner not in {"live", "record"}:
        return {"env_file": None}
    environment = _environment(env_file)
    selected_model = environment.get("LLM_MODEL", model) if model == "scripted-bank-v1" else model
    return {
        "env_file": env_file,
        "api_key": dotenv_credential(env_file),
        "reasoning_effort": "low"
        if selected_model.removeprefix("openai/") == "gpt-6.1-sol"
        else environment.get("LLM_REASONING_EFFORT"),
    }
