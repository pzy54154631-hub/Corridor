"""Provider diagnostics cannot persist a credential echoed outside model output."""

import httpx
import pytest

from corridor.agent.llm import LLMClient, LLMOutputError
from corridor.bench.bank_cases import write_json
from corridor.crypto.jcs import jcs_bytes

CREDENTIAL = "SYNTHETIC_FIX_L1_METADATA_CREDENTIAL"


@pytest.mark.parametrize("api", ["responses", "chat"])
@pytest.mark.parametrize("failed", [False, True])
def test_provider_credential_echo_never_reaches_trace_files_or_errors(
    api, failed, tmp_path, monkeypatch, capsys
):
    for name in tuple(__import__("os").environ):
        if name.startswith("LLM_"):
            monkeypatch.delenv(name)

    def send(_self, request):
        assert request.headers["authorization"] == f"Bearer {CREDENTIAL}"
        body = {
            "model": f"echo-{CREDENTIAL}-provider",
            "status": "completed",
            "output": [
                {
                    "type": "function_call",
                    "name": "emit_result",
                    "arguments": '{"ok":true}',
                }
            ],
            "usage": {"input_tokens": 30, "output_tokens": 8, "total_tokens": 38},
        }
        if api == "chat":
            body.update(
                choices=[
                    {
                        "index": 0,
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "synthetic-call",
                                    "type": "function",
                                    "function": {"name": "emit_result", "arguments": '{"ok":true}'},
                                }
                            ],
                        },
                    }
                ],
                usage={"prompt_tokens": 30, "completion_tokens": 8, "total_tokens": 38},
            )
        if failed:
            body["error"] = dict.fromkeys(("message", "type", "code", "param"), CREDENTIAL)
        return httpx.Response(401 if failed else 200, json=body)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", send)
    client = LLMClient(
        mode="record",
        replay_dir=tmp_path / "recording",
        model="synthetic-pinned-model",
        api=api,
        api_key=CREDENTIAL,
        max_calls=1,
        max_usd="10",
        env_file=None,
    )
    request = dict(
        task_id="FIX-L1-DEVELOPMENT",
        step="credential-metadata",
        prompt={
            "response_schema": {
                "type": "object",
                "properties": {"ok": {"type": "boolean"}},
                "required": ["ok"],
                "additionalProperties": False,
            }
        },
        tool_state={},
        scripted=lambda: {"ok": True},
    )
    if failed:
        with pytest.raises(LLMOutputError) as error:
            client.complete(**request)
        assert CREDENTIAL not in str(error.value)
    else:
        assert client.complete(**request).output == {"ok": True}
    assert "returned_model" not in client.calls[0]
    write_json(tmp_path / "model-calls.json", client.calls)
    captured = capsys.readouterr()
    assert CREDENTIAL not in captured.out + captured.err
    assert CREDENTIAL not in str(client.calls)
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert CREDENTIAL.encode() not in path.read_bytes()


def test_paid_probe_report_omits_echoed_credential_model(tmp_path, monkeypatch):
    from corridor.bench.request_probe import probe_requests

    source = tmp_path / "credential.env"
    source.write_text(
        "LLM_PROVIDER=openai\nLLM_API_BASE=https://api.openai.com/v1\n"
        f"OPENAI_API_KEY={CREDENTIAL}\n",
        encoding="utf-8",
    )
    original = source.read_bytes()

    def send(_self, _request):
        return httpx.Response(
            200,
            json={
                "model": CREDENTIAL,
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

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", send)
    output = tmp_path / "output"
    result = probe_requests("openai", output=output / "requests.json", env_file=source)
    assert result["gate_passed"]
    assert CREDENTIAL.encode() not in jcs_bytes(result)
    assert source.read_bytes() == original
    for path in output.rglob("*"):
        if path.is_file():
            assert CREDENTIAL.encode() not in path.read_bytes()
