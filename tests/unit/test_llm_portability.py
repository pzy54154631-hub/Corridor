"""Local HTTP protocol checks; no repository credentials or real model calls."""

import builtins
import os
from contextlib import contextmanager
from copy import deepcopy
from fractions import Fraction
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from types import SimpleNamespace

import pytest

from corridor.agent.llm import (
    BudgetExceeded,
    LLMClient,
    LLMConfigurationError,
    LLMOutputError,
    ReplayMiss,
)
from corridor.bench.recording import replay_client
from corridor.crypto.jcs import jcs_bytes
from corridor.schemas.base import loads_exact


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    for name in tuple(os.environ):
        if name.startswith("LLM_"):
            monkeypatch.delenv(name)


@contextmanager
def server(*, mutate=None, status=200):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = loads_exact(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append((self.path, body))
            function = body["tools"][0]["function"]
            response = {
                "id": "chatcmpl_synthetic",
                "object": "chat.completion",
                "created": 1,
                "model": body["model"],
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_synthetic",
                                    "type": "function",
                                    "function": {
                                        "name": function["name"],
                                        "arguments": '{"contact_ref":"CONTACT_SYNTHETIC"}',
                                    },
                                }
                            ],
                        },
                    }
                ],
                "usage": {"prompt_tokens": 30, "completion_tokens": 8, "total_tokens": 38},
            }
            if mutate:
                mutate(response)
            payload = jcs_bytes(response)
            self.send_response(status)
            if status == 302:
                self.send_header("Location", "https://forbidden.invalid/v1/chat/completions")
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=http.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield SimpleNamespace(base=f"http://127.0.0.1:{http.server_port}/v1", requests=requests)
    finally:
        http.shutdown()
        http.server_close()
        thread.join()


def client(tmp_path, base, **changes):
    options = dict(
        mode="record",
        replay_dir=tmp_path,
        model="deepseek-chat",
        provider="deepseek",
        api_base=base,
        api="chat",
        local_mock=True,
        api_key="synthetic-protocol-credential",
        max_calls=4,
        max_usd="1",
        assumed_price_in="10",
        assumed_price_out="40",
        env_file=None,
    )
    options.update(changes)
    return LLMClient(**options)


def complete(value, *, named=False):
    schema = {
        "type": "object",
        "properties": {"contact_ref": {"type": "string"}},
        "required": ["contact_ref"],
        "additionalProperties": False,
    }
    prompt = {"response_schema": schema}
    if named:
        prompt = {
            "tools": [
                {
                    "type": "function",
                    "name": "capture_goal",
                    "description": "Use a handle.",
                    "parameters": schema,
                    "strict": True,
                }
            ]
        }
    return value.complete(
        task_id="J01",
        step="protocol",
        prompt=prompt,
        tool_state={"handles": ["CONTACT_SYNTHETIC"]},
        scripted=lambda: {"contact_ref": "CONTACT_SYNTHETIC"},
    )


@pytest.mark.parametrize(
    "model,provider", [("deepseek-chat", "deepseek"), ("qwen-plus", "dashscope")]
)
@pytest.mark.parametrize("named", [False, True])
def test_real_local_http_tool_call_records_and_replays_without_sdk(
    tmp_path, monkeypatch, model, provider, named
):
    with server() as mock:
        recorder = client(tmp_path, mock.base, model=model, provider=provider)
        result = complete(recorder, named=named)
        assert result.source == "local-mock-recorded"
        expected = {"contact_ref": "CONTACT_SYNTHETIC"}
        assert result.output == (
            {"tool": "capture_goal", "arguments": expected} if named else expected
        )
        assert len(mock.requests) == recorder.api_calls == 1
        path, body = mock.requests[0]
        assert path == "/v1/chat/completions"
        assert body["model"] == model and body["parallel_tool_calls"] is False
        assert body["tools"][0]["function"]["parameters"]["additionalProperties"] is False
        assert body["max_tokens"] == 512
        saved = loads_exact(next(tmp_path.rglob("*.json")).read_bytes())
        assert saved["source"] == "local-mock-recorded"
        assert saved["request"]["provider"] == provider
        assert "api_base_hash" in saved["request"] and "api_base" not in saved["request"]
        replay = replay_client(recorder)
    original = builtins.__import__

    def forbidden(name, *args, **kwargs):
        assert name.split(".")[0] not in {"openai", "httpx", "litellm"}
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", forbidden)
    assert complete(replay, named=named).output == result.output
    assert replay.api_calls == 0
    with pytest.raises(ReplayMiss):
        complete(client(tmp_path, mock.base, mode="strict", provider="other"), named=named)


@pytest.mark.parametrize(
    "attack", ["multiple", "text", "content-type", "name", "float", "usage", "incomplete"]
)
def test_invalid_chat_tool_envelopes_never_record(tmp_path, attack):
    def mutate(response):
        choice = response["choices"][0]
        message = choice["message"]
        if attack == "multiple":
            message["tool_calls"].append(deepcopy(message["tool_calls"][0]))
        elif attack == "text":
            message["content"] = '<think>Call a tool</think>{"contact_ref":"CONTACT_SYNTHETIC"}'
            message["tool_calls"] = []
        elif attack == "content-type":
            message["content"] = {"tool": "unoffered"}
        elif attack == "name":
            message["tool_calls"][0]["function"]["name"] = "unoffered"
        elif attack == "float":
            message["tool_calls"][0]["function"]["arguments"] = '{"amount":1.5}'
        elif attack == "usage":
            response["usage"]["prompt_tokens"] = True
        else:
            choice["finish_reason"] = "length"

    with server(mutate=mutate) as mock:
        recorder = client(tmp_path, mock.base)
        with pytest.raises(LLMOutputError):
            complete(recorder)
        assert recorder.api_calls == 1 and recorder.spent_usd > 0
        if attack != "usage":
            actual = Fraction(30 * 10 + 8 * 40, 1_000_000)
            assert recorder.spent_usd == recorder.calls[-1]["cost_usd"] == actual
        assert not list(tmp_path.rglob("*.json"))


@pytest.mark.parametrize("named", [False, True])
@pytest.mark.parametrize("commentary", ["Unverified success.", "<think>Inspect first.</think>"])
def test_chat_tool_commentary_never_enters_output_or_replay(tmp_path, named, commentary):
    def mutate(response):
        response["choices"][0]["message"]["content"] = commentary
        response["choices"][0]["message"]["reasoning"] = "Untrusted reasoning."

    with server(mutate=mutate) as mock:
        recorder = client(tmp_path, mock.base)
        observed = []
        recorder.observers.append(lambda stage, value: observed.append((stage, value)))
        result = complete(recorder, named=named)
    expected = {"contact_ref": "CONTACT_SYNTHETIC"}
    assert result.output == ({"tool": "capture_goal", "arguments": expected} if named else expected)
    assert observed[-1] == ("output", result.output)
    assert commentary not in str(observed)
    assert "Untrusted reasoning" not in str(observed)
    assert complete(replay_client(recorder), named=named).output == result.output
    assert all(commentary.encode() not in path.read_bytes() for path in tmp_path.rglob("*.json"))


def test_overspend_is_counted_even_when_chat_output_is_invalid(tmp_path):
    def mutate(response):
        response["usage"].update(prompt_tokens=1_000_000, total_tokens=1_000_008)
        response["choices"] = []

    with server(mutate=mutate) as mock:
        recorder = client(tmp_path, mock.base)
        with pytest.raises(BudgetExceeded):
            complete(recorder)
        with pytest.raises(BudgetExceeded):
            complete(recorder)
        assert len(mock.requests) == recorder.api_calls == 1


@pytest.mark.parametrize(
    "base",
    [
        "https://host.invalid/v1?token=x",
        "http://remote.invalid/v1",
        "https://u:p@host.invalid/v1",
        "https://host.invalid/v1#fragment",
    ],
)
def test_invalid_base_is_rejected_before_transport(tmp_path, base):
    with pytest.raises(LLMConfigurationError):
        client(tmp_path, base, local_mock=False)


def test_custom_provider_requires_both_explicit_assumed_prices(tmp_path):
    with pytest.raises(LLMConfigurationError, match="prices"):
        client(tmp_path, "https://provider.invalid/v1", local_mock=False, assumed_price_out=None)


def test_redirect_is_not_followed_and_mock_source_cannot_replay_as_live(tmp_path):
    with server(status=302) as mock:
        with pytest.raises(LLMOutputError):
            complete(client(tmp_path / "redirect", mock.base))
        assert len(mock.requests) == 1
        assert not list((tmp_path / "redirect").rglob("*.json"))
    with server() as mock:
        complete(client(tmp_path / "record", mock.base))
        with pytest.raises(ReplayMiss):
            complete(client(tmp_path / "record", mock.base, mode="strict", local_mock=False))
        with pytest.raises(ReplayMiss):
            complete(client(tmp_path / "record", mock.base + "/different", mode="strict"))


def test_portable_configuration_can_come_from_environment_without_file(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_API_BASE", "https://synthetic.invalid/compatible-mode/v1")
    monkeypatch.setenv("LLM_PROVIDER", "dashscope")
    recorder = client(tmp_path, None, provider=None, local_mock=False)
    assert recorder.api_base == "https://synthetic.invalid/compatible-mode/v1"
    assert recorder.provider == "dashscope"


def test_chat_and_configured_responses_share_exact_tool_parameters(tmp_path, monkeypatch):
    from tests.unit.test_llm_responses import _transport

    with server() as mock:
        complete(client(tmp_path / "chat", mock.base))
        chat_schema = mock.requests[0][1]["tools"][0]["function"]["parameters"]
    sends = _transport(monkeypatch, output={"contact_ref": "CONTACT_SYNTHETIC"})
    configured = client(
        tmp_path / "responses",
        "http://127.0.0.1:8123/custom/v1",
        api="responses",
        model="gpt-6.1-sol",
        provider="openai",
    )
    assert complete(configured).source == "local-mock-recorded"
    assert sends[0][0] == "http://127.0.0.1:8123/custom/v1/responses"
    assert sends[0][1]["tools"][0]["parameters"] == chat_schema


@pytest.mark.parametrize("attack", ["base", "model", "schema", "retry", "cached"])
def test_portable_transport_blocks_sdk_scope_drift(tmp_path, monkeypatch, attack):
    import openai

    with server() as mock:

        class AlteredSDK:
            def __init__(self, **settings):
                self.http = settings["http_client"]
                self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

            def create(self, **body):
                # The real SDK merges extension fields into the serialized request body.
                body.update(body.pop("extra_body", {}))
                if attack == "cached":
                    return {}
                target = mock.base + "/chat/completions"
                if attack == "base":
                    target = mock.base + "/unexpected"
                elif attack == "model":
                    body["model"] = "unexpected"
                elif attack == "schema":
                    body["tools"][0]["function"]["parameters"]["additionalProperties"] = True
                self.http.post(target, json=body)
                if attack == "retry":
                    self.http.post(target, json=body)

        monkeypatch.setattr(openai, "OpenAI", AlteredSDK)
        with pytest.raises(LLMOutputError):
            complete(client(tmp_path, mock.base))
        assert len(mock.requests) == (1 if attack == "retry" else 0)
        assert not list(tmp_path.rglob("*.json"))
