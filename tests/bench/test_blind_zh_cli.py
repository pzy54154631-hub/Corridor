"""CLI profiles are frozen without credentials; test never calls a model service."""

from pathlib import Path

import pytest

from corridor.agent.llm import LLMConfigurationError
from corridor.cli.blind_zh import create_client
from corridor.schemas.base import loads_exact


def options(tmp_path):
    return dict(
        planner="live",
        model="gpt-6.1-sol",
        config="A5",
        seed=2,
        user_policy="goal_bound",
        replay_dir=tmp_path / "replay",
        max_calls=3,
        max_usd="1",
        env_file=tmp_path / ".env",
    )


def test_api_profile_records_repeat_without_claiming_unsupported_responses_seed(tmp_path):
    config = options(tmp_path)
    config["env_file"].write_text("LLM_API_KEY=synthetic-test-secret\n", encoding="utf-8")
    client = create_client(**config)
    assert client.seed is None
    assert client.deployment_profile["repeat_seed"] == 2
    data = (config["replay_dir"] / "client-profile.json").read_text(encoding="utf-8")
    assert "synthetic-test-secret" not in data
    strict = create_client(**config, replay="strict")
    assert strict.mode == "strict"
    assert strict.deployment_profile == client.deployment_profile
    config["seed"] = 3
    with pytest.raises(LLMConfigurationError):
        create_client(**config, replay="strict")


def test_verified_local_alias_uses_actual_seed_and_no_real_credential(tmp_path, monkeypatch):
    config = options(tmp_path)
    config["model"] = "qwen3.8-27b"
    config["env_file"].write_text("LLM_API_BASE=http://127.0.0.1:11434/v1\n", encoding="utf-8")
    monkeypatch.setattr(
        "corridor.bench.request_probe.local_deployments",
        lambda: [
            {"model": "qwen3.8:27b-q4_K_M", "weights_digest": "frozen", "context_window": 8192}
        ],
    )
    client = create_client(**config)
    assert client.model == "qwen3.8:27b-q4_K_M"
    assert client.seed == 2
    assert client.provider == "ollama"
    assert client.api == "chat"
    profile = loads_exact((config["replay_dir"] / "client-profile.json").read_bytes())
    assert "api_key" not in profile


def test_scripted_cli_does_not_read_dotenv_or_probe_deployment(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "corridor.cli.blind_zh._environment", lambda _: pytest.fail("Unexpected dotenv read")
    )
    config = options(tmp_path)
    config.update(planner="scripted", model="", env_file=Path("missing"))
    assert create_client(**config).mode == "scripted"


def test_named_file_credentials_and_low_reasoning_survive_record_replay(tmp_path, monkeypatch):
    import httpx

    from corridor.bench.recording import network_disabled

    config = options(tmp_path)
    credential = b"SYNTHETIC_FIX_L1_FILE_KEY"
    original = b"OPENAI_API_KEY=" + credential + b"\n"
    config["env_file"].write_bytes(original)
    monkeypatch.setenv("LLM_API_KEY", "SYNTHETIC_INHERITED_KEY")
    sends = []

    def send(_self, request):
        assert request.headers["authorization"] == "Bearer " + credential.decode()
        sends.append(loads_exact(request.content))
        return httpx.Response(
            200,
            json={
                "id": "resp_fix_l1",
                "object": "response",
                "model": "gpt-6.1-sol",
                "status": "completed",
                "output": [
                    {
                        "id": "msg_fix_l1",
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": '{"handle":"PROBE_OK"}'}],
                    }
                ],
                "usage": {"input_tokens": 30, "output_tokens": 8, "total_tokens": 38},
            },
        )

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", send)
    request = dict(
        task_id="FIX-L1-DEVELOPMENT",
        step="credential-isolation",
        prompt={"instruction": "Return the permitted handle."},
        tool_state={"handle": "PROBE_OK"},
        scripted=lambda: {"handle": "PROBE_OK"},
    )
    client = create_client(**config)
    recorded = client.complete(**request)
    with network_disabled():
        strict = create_client(**config, replay="strict")
        replayed = strict.complete(**request)
    assert replayed.output == recorded.output
    assert replayed.replay_key == recorded.replay_key
    assert len(sends) == 1
    assert sends[0]["reasoning"] == {"effort": "low"}
    assert client.deployment_profile["reasoning_effort"] == "low"
    assert config["env_file"].read_bytes() == original
    for path in config["replay_dir"].rglob("*"):
        if path.is_file():
            content = path.read_bytes()
            assert credential not in content
            assert b"SYNTHETIC_INHERITED_KEY" not in content
            assert str(config["env_file"]).encode() not in content


def test_blind_cli_exposes_env_file_without_echoing_or_copying_it(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from corridor.cli import app
    from corridor.cli import blind_zh as command

    seen = []
    path = tmp_path / "outside credentials.env"
    monkeypatch.setattr(command, "create_client", lambda **kwargs: seen.append(kwargs))
    monkeypatch.setattr(
        command,
        "run_blind_zh",
        lambda *args, **kwargs: {
            "planned": 0,
            "valid_completed": 0,
            "runtime_errors": 0,
            "not_run": 0,
            "unsupported_oracle": 0,
            "invalid_case": 0,
        },
    )
    result = CliRunner().invoke(app, ["blind-zh", "--env-file", str(path)])
    assert result.exit_code == 0, result.output
    assert seen[0]["env_file"] == path
    assert str(path) not in result.output
    assert not path.exists()


@pytest.mark.parametrize("passed,exit_code", [(True, 0), (False, 1)])
def test_validate_cli_only_reports_file_identifiers_and_status(
    tmp_path, monkeypatch, passed, exit_code
):
    from typer.testing import CliRunner

    from corridor.cli import app
    from corridor.cli import blind_zh as command

    def validate(path):
        assert path == tmp_path
        return [{"file_id": "synthetic-01", "passed": passed}]

    monkeypatch.setattr(command, "validate_cases", validate, raising=False)
    monkeypatch.setattr(
        command, "create_client", lambda **kwargs: pytest.fail("Validation created a client")
    )
    result = CliRunner().invoke(
        app, ["blind-zh", "--validate", "--cases", str(tmp_path), "--planner", "live"]
    )
    assert result.exit_code == exit_code, result.output
    assert result.output == f"synthetic-01: {'PASS' if passed else 'FAIL'}\n"
    assert not list(tmp_path.iterdir())


def test_validate_cli_empty_input_fails_without_creating_evidence(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from corridor.cli import app
    from corridor.cli import blind_zh as command

    monkeypatch.setattr(command, "validate_cases", lambda _: [], raising=False)
    result = CliRunner().invoke(app, ["blind-zh", "--validate", "--cases", str(tmp_path)])
    assert result.exit_code == 1
    assert not list(tmp_path.iterdir())
