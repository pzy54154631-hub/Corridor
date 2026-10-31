"""Named credentials stay runtime-only across the domestic live entry points."""

import runpy
import sys
from pathlib import Path
from types import ModuleType

import httpx
import pytest
from typer.testing import CliRunner

from corridor.cli import app
from corridor.crypto.jcs import jcs_bytes
from corridor.schemas.base import loads_exact

SENTINEL = "SYNTHETIC_FIX_L1_ENTRY_CREDENTIAL"
INHERITED = "SYNTHETIC_FIX_L1_INHERITED_CREDENTIAL"


def credentials(tmp_path, monkeypatch):
    path = tmp_path / "external credentials.env"
    path.write_text(
        f"OPENAI_API_KEY={SENTINEL}\nLLM_MAX_CALLS=3\nLLM_MAX_USD=10\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("LLM_API_KEY", INHERITED)
    return path, path.read_bytes()


def assert_private(output, env_file, original, captured=""):
    assert env_file.read_bytes() == original
    assert SENTINEL not in captured and INHERITED not in captured
    for path in output.rglob("*"):
        if path.is_file():
            content = path.read_bytes()
            assert SENTINEL.encode() not in content
            assert INHERITED.encode() not in content
            assert str(env_file).encode() not in content


@pytest.mark.parametrize("command", ["interact-eval", "consistency"])
def test_cli_forwards_env_file_without_reading_or_echoing_it(command, tmp_path, monkeypatch):
    import corridor.bench.consistency as consistency
    import corridor.cli.interact_eval as interaction

    seen = []

    def run(*args, **kwargs):
        seen.append(kwargs)
        return {"summary": {"valid_completed": 0, "planned": 0}}

    target, name = (
        (interaction, "run_interact_eval")
        if command == "interact-eval"
        else (consistency, "run_consistency")
    )
    monkeypatch.setattr(target, name, run)
    path = tmp_path / "outside credentials.env"
    result = CliRunner().invoke(
        app, [command, "--env-file", str(path), "--output", str(tmp_path / "output")]
    )
    assert result.exit_code == 0, result.output
    assert seen[0]["env_file"] == path
    assert str(path) not in result.output and not path.exists()


def test_interaction_file_key_is_read_only_and_absent_from_all_outputs(tmp_path, monkeypatch):
    env_file, original = credentials(tmp_path, monkeypatch)
    sends = []

    def transport(_self, request):
        assert request.headers["authorization"] == f"Bearer {SENTINEL}"
        sends.append(loads_exact(request.content))
        return httpx.Response(
            200,
            json={
                "model": "gpt-6.1-sol",
                "status": "completed",
                "output": [
                    {
                        "type": "function_call",
                        "name": "emit_result",
                        "arguments": '{"kind":"transfer","target_role":"MOM"}',
                    }
                ],
                "usage": {"input_tokens": 30, "output_tokens": 8, "total_tokens": 38},
            },
        )

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", transport)
    output = tmp_path / "interaction"
    result = CliRunner().invoke(
        app,
        [
            "interact-eval",
            "--planner",
            "live",
            "--model",
            "gpt-6.1-sol",
            "--cases",
            "ZH-TR-01",
            "--env-file",
            str(env_file),
            "--output",
            str(output),
        ],
    )
    assert result.exit_code == 0, result.output
    assert sends and all(row["reasoning"] == {"effort": "low"} for row in sends)
    assert loads_exact((output / "results.json").read_bytes())["summary"]["valid_completed"] == 3
    assert_private(output, env_file, original, result.output)


def test_intent_cli_file_key_is_absent_from_recording_and_report(tmp_path, monkeypatch):
    import corridor.cli.intent_eval as command

    env_file, original = credentials(tmp_path, monkeypatch)
    sends = []

    def transport(_self, request):
        assert request.headers["authorization"] == f"Bearer {SENTINEL}"
        sends.append(loads_exact(request.content))
        return httpx.Response(
            200,
            json={
                "model": "gpt-6.1-sol",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": '{"handle":"PROBE_OK"}'}],
                    }
                ],
                "usage": {"input_tokens": 30, "output_tokens": 8, "total_tokens": 38},
            },
        )

    def evaluate(*, output, client):
        result = client.complete(
            task_id="FIX-L1-DEVELOPMENT",
            step="intent-credentials",
            prompt={"instruction": "Return the permitted handle."},
            tool_state={"handle": "PROBE_OK"},
            scripted=lambda: {"handle": "PROBE_OK"},
        )
        report = {
            "intent_correct": 1,
            "total": 1,
            "output": result.output,
            "parameter_exact_match": {"accuracy": "1/1"},
            "parameter_accuracy": {},
        }
        output.mkdir(parents=True, exist_ok=True)
        (output / "results.json").write_bytes(jcs_bytes(report))
        return report

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", transport)
    monkeypatch.setattr(command, "run_intent_eval", evaluate)
    output = tmp_path / "intent"
    result = CliRunner().invoke(
        app,
        [
            "intent-eval",
            "--planner",
            "live",
            "--model",
            "gpt-6.1-sol",
            "--env-file",
            str(env_file),
            "--output",
            str(output),
        ],
    )
    assert result.exit_code == 0, result.output
    assert len(sends) == 1 and sends[0]["reasoning"] == {"effort": "low"}
    assert_private(output, env_file, original, result.output)


def test_consistency_keeps_live_runs_behind_formal_coordinator(tmp_path):
    result = CliRunner().invoke(
        app,
        [
            "consistency",
            "--planner",
            "live",
            "--env-file",
            str(tmp_path / "missing.env"),
            "--output",
            str(tmp_path / "output"),
        ],
    )
    assert result.exit_code != 0
    assert "gated run_live_zh coordinator" in str(result.exception)
    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize("command", ["interact-eval", "intent-eval"])
def test_named_missing_file_never_falls_back_to_inherited_key(command, tmp_path, monkeypatch):
    from corridor.agent.llm import LLMConfigurationError

    monkeypatch.setenv("LLM_API_KEY", INHERITED)
    result = CliRunner().invoke(
        app,
        [
            command,
            "--planner",
            "live",
            "--model",
            "gpt-6.1-sol",
            "--env-file",
            str(tmp_path / "missing.env"),
            "--output",
            str(tmp_path / "output"),
        ],
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, LLMConfigurationError)
    assert "credential" in str(result.exception)
    assert INHERITED not in result.output
    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize("planner", ["scripted", "strict"])
def test_non_live_modes_do_not_read_named_file(planner, tmp_path, monkeypatch):
    from corridor.bench import live_credentials

    monkeypatch.setattr(
        live_credentials, "_environment", lambda _path: pytest.fail("Unexpected credential read")
    )
    assert live_credentials.named_env_options(planner, "gpt-6.1-sol", tmp_path / "missing") == {
        "env_file": None
    }


@pytest.mark.parametrize("strict", [False, True])
def test_agentdojo_worker_forwards_credentials_outside_spec(tmp_path, monkeypatch, strict):
    env_file, original = credentials(tmp_path, monkeypatch)
    spec = {"row": {"experiment": "E4"}}
    source = tmp_path / "worker.json"
    source.write_bytes(jcs_bytes(spec))
    output = tmp_path / "results" / "episode.json"
    observed = []
    module = ModuleType("run_live_zh")

    def worker(actual, destination, mode, *, env_file):
        observed.append((actual, mode, env_file))
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(jcs_bytes({"status": "valid_completed"}))

    module.worker = worker
    monkeypatch.setitem(sys.modules, "run_live_zh", module)
    script = Path(__file__).resolve().parents[2] / "scripts/agentdojo_run.py"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(script),
            "--worker",
            str(source),
            "--output",
            str(output),
            "--env-file",
            str(env_file),
            *(["--strict"] if strict else []),
        ],
    )
    runpy.run_path(str(script), run_name="__main__")
    assert observed == [(spec, "strict" if strict else "record", env_file)]
    assert loads_exact(source.read_bytes()) == spec
    assert_private(output.parent, env_file, original)
