"""Request probes report facts and never turn unavailable transport into support."""

from pathlib import Path

import httpx
from typer.testing import CliRunner

from corridor.bench.request_probe import classify_parameters, dotenv_credential
from corridor.cli import app
from corridor.crypto.jcs import jcs_bytes
from corridor.schemas.base import loads_exact


def test_local_token_probe_preserves_prompt_and_uses_native_counts(monkeypatch):
    from corridor.agent.llm import local_prompt_token_probe

    wire = {
        "model": "qwen3.8:27b-q4_K_M",
        "messages": [{"role": "user", "content": "Synthetic request"}],
        "tools": [{"type": "function", "function": {"name": "synthetic"}}],
        "reasoning_effort": "none",
        "max_tokens": 512,
        "seed": 1,
    }
    sent = []

    def send(_self, request):
        sent.append((str(request.url), loads_exact(request.content)))
        return httpx.Response(200, json={"prompt_eval_count": 123, "eval_count": 1, "done": True})

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", send)
    assert local_prompt_token_probe(wire) == {"prompt_eval_count": 123, "eval_count": 1}
    url, body = sent[0]
    assert url == "http://127.0.0.1:11434/api/chat"
    assert body["messages"] == wire["messages"] and body["tools"] == wire["tools"]
    assert body["think"] is False and body["options"] == {"num_predict": 1, "seed": 1}
    assert "num_ctx" not in str(body)


def test_probe_classifies_captured_wire_not_requested_config():
    fields = classify_parameters({"seed": 3, "reasoning_effort": "none"}, api="chat")
    assert fields["seed"]["status"] == "supported"
    assert fields["reasoning_effort"]["sent"] is True
    assert fields["temperature"]["value"] == "provider-default"
    assert fields["context"]["status"] == "unsupported"
    assert fields["context"]["sent"] is False
    assert classify_parameters({}, api="responses")["seed"]["status"] == "unsupported"


def test_probe_key_only_comes_from_named_file(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "DO_NOT_USE_ENVIRONMENT")
    assert dotenv_credential(tmp_path / "missing") == ""
    path = tmp_path / "credential.env"
    path.write_text('LLM_API_KEY="SYNTHETIC_FILE_ONLY"\n', encoding="utf-8")
    assert dotenv_credential(path) == "SYNTHETIC_FILE_ONLY"


def test_probe_cli_passes_explicit_env_file_without_reading_or_echoing_it(tmp_path, monkeypatch):
    from corridor.cli import probe_requests as command

    env_file = tmp_path / "external credentials.env"
    output = tmp_path / "requests.json"
    seen = []

    def probe(provider, **kwargs):
        seen.append((provider, kwargs))
        return {"gate_passed": True}

    monkeypatch.setattr(command, "probe_requests", probe)
    result = CliRunner().invoke(
        app,
        [
            "probe-requests",
            "--provider",
            "openai",
            "--env-file",
            str(env_file),
            "--output",
            str(output),
        ],
    )
    assert result.exit_code == 0, result.output
    assert seen == [("openai", {"output": output, "env_file": env_file})]
    assert str(env_file) not in result.output
    assert not env_file.exists()


def test_probe_cli_preserves_worktree_default_env_file(tmp_path, monkeypatch):
    from corridor.cli import probe_requests as command

    seen = []

    def probe(provider, **kwargs):
        seen.append((provider, kwargs))
        return {"gate_passed": False}

    monkeypatch.setattr(command, "probe_requests", probe)
    output = tmp_path / "requests.json"
    result = CliRunner().invoke(
        app, ["probe-requests", "--provider", "openai", "--output", str(output)]
    )
    assert result.exit_code == 1
    assert seen == [("openai", {"output": output, "env_file": Path(".env")})]


def test_discovery_failure_is_retained_as_closed_gate(tmp_path, monkeypatch):
    from corridor.bench import request_probe

    def unavailable(_path):
        raise OSError("Synthetic local discovery failure")

    monkeypatch.setattr(request_probe, "local_deployments", unavailable)
    output = tmp_path / "requests.json"
    result = request_probe.probe_requests("local", output=output)
    assert result["gate_passed"] is False
    assert result["failure_reason"] == "OSError"
    assert loads_exact(output.read_bytes())["providers"]["local"] == result


def test_paid_probe_never_exceeds_three_retained_attempts(tmp_path, monkeypatch):
    from corridor.bench import request_probe

    env = tmp_path / ".env"
    env.write_text(
        "LLM_PROVIDER=openai\nLLM_API_BASE=https://api.openai.com/v1\nLLM_API_KEY=SYNTHETIC_ONLY\n",
        encoding="utf-8",
    )
    output = tmp_path / "requests.json"
    output.write_bytes(
        jcs_bytes({
            "providers": {
                "openai": {
                    "models": [
                        {
                            "model": "gpt-6.1-sol",
                            "status": "runtime_error",
                            "actual_outbound_calls": 3,
                        }
                    ]
                }
            },
        })
    )

    def forbidden(**_kwargs):
        raise AssertionError("Must not instantiate another paid transport")

    monkeypatch.setattr(request_probe, "LLMClient", forbidden)
    result = request_probe.probe_requests("openai", output=output, env_file=env)
    assert result["gate_passed"] is False
    assert result["models"][0]["status"] == "not_run"
    assert result["failure_reason"] == "paid_probe_three_call_limit"


def test_openai_probe_uses_adopted_low_reasoning_and_records_strict_replay(tmp_path, monkeypatch):
    from corridor.bench import request_probe

    env_file = tmp_path / "credentials.env"
    env_file.write_text(
        "LLM_PROVIDER=openai\nLLM_API_BASE=https://api.openai.com/v1\nOPENAI_API_KEY=SYNTHETIC_FILE_ONLY\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("LLM_API_KEY", "DO_NOT_USE_ENVIRONMENT")
    sends = []

    def send(_self, request):
        assert request.headers["authorization"] == "Bearer SYNTHETIC_FILE_ONLY"
        sends.append(loads_exact(request.content))
        return httpx.Response(
            200,
            json={
                "id": "resp_probe",
                "object": "response",
                "model": "gpt-6.1-sol",
                "status": "completed",
                "output": [
                    {
                        "id": "msg_probe",
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": '{"handle":"PROBE_OK"}'}],
                    }
                ],
                "usage": {"input_tokens": 30, "output_tokens": 8, "total_tokens": 38},
            },
        )

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", send)
    output = tmp_path / "requests.json"
    local_result = {"gate_passed": True, "models": []}
    output.write_bytes(jcs_bytes({"providers": {"local": local_result}}))
    result = request_probe.probe_requests("openai", output=output, env_file=env_file)
    assert result["gate_passed"] is True
    assert len(sends) == 1
    assert sends[0]["reasoning"] == {"effort": "low"}
    assert sends[0]["max_output_tokens"] == 128
    row = result["models"][0]
    assert row["deployment_profile"]["reasoning_effort"] == "low"
    assert row["strict_replay"] is True
    assert row["actual_outbound_calls"] == 1
    assert loads_exact(output.read_bytes())["providers"]["local"] == local_result
    assert b"SYNTHETIC_FILE_ONLY" not in output.read_bytes()
    assert b"DO_NOT_USE_ENVIRONMENT" not in output.read_bytes()
