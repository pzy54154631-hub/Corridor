"""Domestic probes freeze official prices and retain only sanitized transport facts."""

from hashlib import sha256

import httpx
import pytest

from corridor.bench import request_probe
from corridor.crypto.jcs import jcs_bytes
from corridor.schemas.base import loads_exact


def _env(tmp_path, provider, model):
    base = (
        "https://api.deepseek.com/v1"
        if provider == "deepseek"
        else "https://open.bigmodel.cn/api/paas/v4"
    )
    path = tmp_path / f"{provider}.env"
    path.write_text(
        f"LLM_PROVIDER={provider}\nLLM_API_BASE={base}\nLLM_MODEL={model}\nLLM_API_KEY=SYNTHETIC-{provider}-ONLY\n",
        encoding="utf-8",
    )
    return path


def _prices(provider, model):
    if provider == "deepseek":
        return (
            f"<table><tr><th>模型</th><th>{model}</th></tr>"
            "<tr><td>百万tokens输入（缓存未命中）</td><td>2元</td></tr>"
            "<tr><td>百万tokens输出</td><td>8元</td></tr></table>"
        ).encode()
    return (
        "<table><tr><th>模型名称</th><th>上下文</th><th>输入单价（元/百万 Tokens）</th>"
        "<th>输出单价（元/百万 Tokens）</th></tr>"
        f"<tr><td>{model}</td><td>输入长度 [0,32K)</td><td>6</td><td>24</td></tr>"
        f"<tr><td>{model}</td><td>输入长度 ≥32K</td><td>8</td><td>28</td></tr></table>"
    ).encode()


@pytest.mark.parametrize("provider,model", [("deepseek", "deepseek-chat"), ("zhipu", "glm-5.1")])
def test_domestic_probe_binds_one_request_and_replays_offline(
    tmp_path, monkeypatch, provider, model
):
    env = _env(tmp_path, provider, model)
    sends = []
    page = _prices(provider, model)

    def send(_self, request):
        if request.method == "GET":
            assert "authorization" not in request.headers
            return httpx.Response(200, content=page, headers={"content-type": "text/html"})
        sends.append(request)
        assert request.headers["authorization"] == f"Bearer SYNTHETIC-{provider}-ONLY"
        return httpx.Response(
            200,
            json={
                "id": "synthetic",
                "object": "chat.completion",
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "synthetic",
                                    "type": "function",
                                    "function": {
                                        "name": "emit_result",
                                        "arguments": '{"kind":"transfer","target_role":"MOM",'
                                        '"assistant_text":"","fact_refs":[]}',
                                    },
                                }
                            ],
                        },
                    }
                ],
                "usage": {"prompt_tokens": 30, "completion_tokens": 8, "total_tokens": 38},
            },
        )

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", send)
    monkeypatch.setenv("LLM_API_KEY", "SYNTHETIC_OPENAI_ENV")
    output = tmp_path / "requests.json"
    result = request_probe.probe_requests(provider, output=output, env_file=env)
    assert result["gate_passed"] and result["provider"] == provider
    assert len(sends) == 1
    assert result["pricing_currency"] == "CNY"
    assert result["pricing_evidence"]["page_sha256"] == sha256(page).hexdigest()
    assert result["pricing_evidence"]["fetched_at_utc"]
    assert result["pricing_evidence"]["excerpt"]
    assert result["price_in_per_million"] == ("2/1" if provider == "deepseek" else "8/1")
    assert result["models"][0]["strict_replay"]
    assert result["models"][0]["parameters"]["seed"]["status"] == "unsupported"
    for path in tmp_path.rglob("*"):
        if path.is_file() and path != env:
            assert f"SYNTHETIC-{provider}-ONLY".encode() not in path.read_bytes()


def test_absent_official_model_price_stops_before_paid_request(tmp_path, monkeypatch):
    env = _env(tmp_path, "deepseek", "deepseek-chat")
    methods = []

    def send(_self, request):
        methods.append(request.method)
        assert request.method == "GET"
        return httpx.Response(200, content=_prices("deepseek", "different-model"))

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", send)
    result = request_probe.probe_requests(
        "deepseek", output=tmp_path / "requests.json", env_file=env
    )
    assert not result["gate_passed"]
    assert result["failure_reason"] == "official_model_price_unavailable"
    assert result["pricing_evidence"]["page_sha256"]
    assert result["models"][0]["actual_outbound_calls"] == 0
    assert methods == ["GET"]


def test_invalid_zhipu_model_retains_sanitized_original_error(tmp_path, monkeypatch):
    env = _env(tmp_path, "zhipu", "glm-5.1")
    calls = []

    def send(_self, request):
        if request.method == "GET":
            return httpx.Response(200, content=_prices("zhipu", "glm-5.1"))
        calls.append(request)
        return httpx.Response(
            400,
            json={
                "error": {
                    "code": "1211",
                    "message": "模型不存在，请检查模型代码。 SYNTHETIC-zhipu-ONLY",
                }
            },
        )

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", send)
    output = tmp_path / "requests.json"
    result = request_probe.probe_requests("zhipu", output=output, env_file=env)
    assert not result["gate_passed"] and len(calls) == 1
    error = result["models"][0]["provider_error"]
    assert error["message"].startswith("模型不存在，请检查模型代码。")
    assert error["code"] == "1211" and error["http_status"] == 400
    assert b"SYNTHETIC-zhipu-ONLY" not in output.read_bytes()


def test_domestic_probe_enforces_provider_specific_lifetime_limit(tmp_path, monkeypatch):
    env = _env(tmp_path, "zhipu", "glm-5.1")
    output = tmp_path / "requests.json"
    output.write_bytes(
        jcs_bytes({
            "providers": {
                "zhipu": {
                    "models": [
                        {"model": "glm-5.1", "status": "runtime_error", "actual_outbound_calls": 1}
                    ]
                }
            },
            "retained_attempts": {"zhipu": [{"models": [{"actual_outbound_calls": 2}]}]},
        })
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("No request is permitted after the paid cap")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    result = request_probe.probe_requests("zhipu", output=output, env_file=env)
    assert result["failure_reason"] == "paid_probe_three_call_limit"
    assert result["models"][0]["actual_outbound_calls"] == 0
    assert loads_exact(output.read_bytes())["providers"]["zhipu"] == result


def test_provider_error_redacts_partial_key_echo():
    secret = "FAKE-abcdefgh-12345678"
    text = request_probe._redact_provider_message(
        "模型不存在。 key abcdefgh; suffix 5678; Bearer secret-value", secret
    )
    assert "abcdefgh" not in text and "5678" not in text and "secret-value" not in text
    assert text.startswith("模型不存在。")


def test_probe_cli_requires_domestic_binding_and_never_reuses_openai_file(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from corridor.cli import app
    from corridor.cli import probe_requests as command

    seen = []

    def probe(provider, **kwargs):
        seen.append((provider, kwargs["env_file"]))
        return {"gate_passed": True}

    monkeypatch.setattr(command, "probe_requests", probe)
    runner = CliRunner()
    rejected = runner.invoke(
        app,
        ["probe-requests", "--provider", "deepseek", "--env-file", str(tmp_path / "openai.env")],
    )
    assert rejected.exit_code != 0 and not seen
    accepted = runner.invoke(
        app,
        [
            "probe-requests",
            "--provider",
            "deepseek",
            "--env-file",
            str(tmp_path / "openai.env"),
            "--provider-env",
            f"deepseek={tmp_path / 'deepseek.env'}",
        ],
    )
    assert accepted.exit_code == 0
    assert seen == [("deepseek", tmp_path / "deepseek.env")]


def test_probe_reservation_survives_missing_completed_attempt(tmp_path, monkeypatch):
    env = _env(tmp_path, "zhipu", "glm-5.1")
    output = tmp_path / "requests.json"
    output.write_bytes(jcs_bytes({"providers": {}, "paid_call_reservations": {"zhipu": 3}}))

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Crash reservations still count against the paid cap")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    result = request_probe.probe_requests("zhipu", output=output, env_file=env)
    assert result["failure_reason"] == "paid_probe_three_call_limit"


@pytest.mark.parametrize("bad_field", ["output", "source", "replay_key"])
def test_equal_but_invalid_probe_or_non_replay_source_never_passes(
    tmp_path, monkeypatch, bad_field
):
    from corridor.agent.llm import LLMResult

    env = _env(tmp_path, "zhipu", "glm-5.1")

    def send(_self, request):
        assert request.method == "GET"
        return httpx.Response(200, content=_prices("zhipu", "glm-5.1"))

    def call(client):
        strict = client.mode == "strict"
        return LLMResult(
            output={"handle": "WRONG"}
            if bad_field == "output"
            else {"kind": "transfer", "target_role": "MOM", "assistant_text": "", "fact_refs": []},
            source="replay" if strict and bad_field != "source" else "live-recorded",
            replay_key="wrong" if strict and bad_field == "replay_key" else "same-key",
        )

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", send)
    monkeypatch.setattr(request_probe, "_formal_call", call)
    result = request_probe.probe_requests("zhipu", output=tmp_path / "requests.json", env_file=env)
    assert not result["gate_passed"]
    assert result["failure_reason"] == "strict_probe_replay_mismatch"


@pytest.mark.parametrize(
    "output_tokens,reasoning_tokens,finish",
    [(301, 0, "tool_calls"), (120, 1, "tool_calls"), (120, 0, "length")],
)
def test_formal_probe_rejects_excess_output_reasoning_or_truncation(
    tmp_path, monkeypatch, output_tokens, reasoning_tokens, finish
):
    env = _env(tmp_path, "deepseek", "deepseek-flash")

    def send(_self, request):
        if request.method == "GET":
            return httpx.Response(200, content=_prices("deepseek", "deepseek-flash"))
        body = loads_exact(request.content)
        assert body["thinking"] == {"type": "disabled"}
        assert body["parallel_tool_calls"] is False
        assert body["tool_choice"]["function"]["name"] == "emit_result"
        return httpx.Response(
            200,
            json={
                "id": "synthetic",
                "model": "deepseek-flash",
                "object": "chat.completion",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": finish,
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "synthetic",
                                    "type": "function",
                                    "function": {
                                        "name": "emit_result",
                                        "arguments": '{"kind":"transfer",'
                                        '"target_role":"MOM","assistant_text":"","fact_refs":[]}',
                                    },
                                }
                            ],
                        },
                    }
                ],
                "usage": {
                    "prompt_tokens": 3000,
                    "completion_tokens": output_tokens,
                    "total_tokens": 3000 + output_tokens,
                    "completion_tokens_details": {"reasoning_tokens": reasoning_tokens},
                },
            },
        )

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", send)
    result = request_probe.probe_requests(
        "deepseek", output=tmp_path / "requests.json", env_file=env
    )
    assert not result["gate_passed"]
    assert result["models"][0]["actual_outbound_calls"] == 1
