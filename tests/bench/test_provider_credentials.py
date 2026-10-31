"""Provider-bound credential files never supply another provider's transport."""

import httpx
import pytest

from corridor.agent.llm import LLMClient
from corridor.bench.request_probe import load_provider_credentials


def credential_file(tmp_path, provider, base, key, *, model="synthetic-model"):
    path = tmp_path / f"{provider}.env"
    path.write_text(
        f"LLM_PROVIDER={provider}\nLLM_API_BASE={base}\nLLM_API_KEY={key}\nLLM_MODEL={model}\n",
        encoding="utf-8",
    )
    return path


def test_provider_binding_rejects_cross_provider_and_endpoint_before_transport(tmp_path):
    path = credential_file(tmp_path, "openai", "https://api.openai.com/v1", "FAKE_OPENAI")
    with pytest.raises(ValueError, match="provider_mismatch"):
        load_provider_credentials(path, provider="deepseek", api_base="https://api.deepseek.com")
    with pytest.raises(ValueError, match="api_base_mismatch"):
        load_provider_credentials(path, provider="openai", api_base="https://api.deepseek.com")


@pytest.mark.parametrize("missing", ["LLM_PROVIDER", "LLM_API_BASE"])
def test_provider_binding_requires_declarations(tmp_path, missing):
    path = credential_file(tmp_path, "deepseek", "https://api.deepseek.com", "FAKE_DEEPSEEK")
    path.write_text(
        "\n".join(
            line
            for line in path.read_text(encoding="utf-8").splitlines()
            if not line.startswith(missing + "=")
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="credential_binding_missing"):
        load_provider_credentials(path, provider="deepseek", api_base="https://api.deepseek.com")


def test_missing_credentials_ignore_process_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "FAKE_INHERITED_OPENAI")
    assert (
        load_provider_credentials(None, provider="deepseek", api_base="https://api.deepseek.com")
        is None
    )
    assert (
        load_provider_credentials(
            tmp_path / "missing", provider="deepseek", api_base="https://api.deepseek.com"
        )
        is None
    )
    path = credential_file(tmp_path, "deepseek", "https://api.deepseek.com", "")
    assert (
        load_provider_credentials(path, provider="deepseek", api_base="https://api.deepseek.com")
        is None
    )


def test_bound_deepseek_transport_never_uses_openai_key(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "FAKE_OPENAI_PROCESS")
    path = credential_file(tmp_path, "deepseek", "https://api.deepseek.com", "FAKE_DEEPSEEK_FILE")
    bound = load_provider_credentials(
        path, provider="deepseek", api_base="https://api.deepseek.com"
    )
    assert bound is not None
    assert "FAKE_DEEPSEEK_FILE" not in repr(bound)
    sent = []

    def send(_self, request):
        sent.append((str(request.url), request.headers["authorization"]))
        return httpx.Response(
            200,
            json={
                "id": "synthetic-response",
                "object": "chat.completion",
                "model": "synthetic-model",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": '{"handle":"PROBE_OK"}'},
                    }
                ],
                "usage": {"prompt_tokens": 30, "completion_tokens": 8, "total_tokens": 38},
            },
        )

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", send)
    client = LLMClient(
        mode="record",
        provider=bound.provider,
        api_base=bound.api_base,
        model=bound.model,
        api="chat",
        api_key=bound.api_key,
        env_file=None,
        max_calls=1,
        max_usd="1",
        assumed_price_in="1",
        assumed_price_out="2",
        replay_dir=tmp_path / "replay",
    )
    from corridor.bench.request_probe import _call

    _call(client)
    assert sent == [("https://api.deepseek.com/chat/completions", "Bearer FAKE_DEEPSEEK_FILE")]
    for path in (tmp_path / "replay").rglob("*"):
        if path.is_file():
            assert b"FAKE_DEEPSEEK_FILE" not in path.read_bytes()


def test_openai_credentials_cannot_bind_to_third_party_even_if_file_agrees(tmp_path):
    path = credential_file(tmp_path, "openai", "https://api.deepseek.com", "FAKE_OPENAI")
    with pytest.raises(ValueError, match="provider_api_base_forbidden"):
        load_provider_credentials(path, provider="openai", api_base="https://api.deepseek.com")


def test_model_binding_rejects_changed_model_and_duplicate_declarations(tmp_path):
    path = credential_file(tmp_path, "deepseek", "https://api.deepseek.com", "FAKE_DEEPSEEK")
    with pytest.raises(ValueError, match="model_mismatch"):
        load_provider_credentials(
            path, provider="deepseek", api_base="https://api.deepseek.com", model="different-model"
        )
    with path.open("a", encoding="utf-8") as stream:
        stream.write("LLM_PROVIDER=openai\n")
    with pytest.raises(ValueError, match="credential_duplicate_field"):
        load_provider_credentials(path, provider="deepseek", api_base="https://api.deepseek.com")
