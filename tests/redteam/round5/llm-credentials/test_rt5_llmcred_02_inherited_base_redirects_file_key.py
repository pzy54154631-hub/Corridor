"""llm-credentials-02: inherited LLM_API_BASE redirects the named file's key.

The live entry points read the *credential* only from `--env-file`
(`dotenv_credential` deliberately ignores inherited keys), but resolve the
*destination* through `_environment(env_file)`, where every inherited `LLM_*`
process variable overrides the named file.  An operator whose shell still
exports another provider's `LLM_API_BASE` / `LLM_PROVIDER` / assumed prices
(the documented env-only portable configuration) and who runs
`corridor blind-zh --planner live --env-file openai.env` sends the OpenAI key
from that file to the other host, even though the file pins the OpenAI base.
The same merge feeds `intent-eval` / `interact-eval` through
`named_env_options`.  No network: the HTTP transport is patched.
"""

import os
from urllib.parse import urlsplit

import httpx
import pytest

from corridor.schemas.base import loads_exact

SECRET = "SYNTHETIC_RT5_FILE_PINNED_OPENAI_KEY_0002"


def _env(tmp_path, monkeypatch):
    for name in tuple(os.environ):
        if name.startswith("LLM_"):
            monkeypatch.delenv(name)
    path = tmp_path / "openai credentials.env"
    path.write_text(
        f"LLM_API_KEY={SECRET}\n"
        "LLM_PROVIDER=openai\n"
        "LLM_API_BASE=https://api.openai.com/v1\n"
        "LLM_MODEL=gpt-6.1-sol\n"
        "LLM_MAX_CALLS=3\n"
        "LLM_MAX_USD=10\n",
        encoding="utf-8",
    )
    # Leftover shell configuration for another OpenAI-compatible vendor.
    monkeypatch.setenv("LLM_API_BASE", "https://other-vendor.invalid/compatible-mode/v1")
    monkeypatch.setenv("LLM_PROVIDER", "othervendor")
    monkeypatch.setenv("LLM_ASSUMED_PRICE_IN", "2")
    monkeypatch.setenv("LLM_ASSUMED_PRICE_OUT", "8")
    return path


def _capture(monkeypatch):
    sent = []

    def send(_self, request):
        body = loads_exact(request.content)
        sent.append((urlsplit(str(request.url)).hostname, request.headers["authorization"]))
        return httpx.Response(
            200,
            json={
                "model": body["model"],
                "status": "completed",
                "output": [
                    {
                        "type": "function_call",
                        "name": "emit_result",
                        "arguments": '{"kind":"clarify","target_role":null}',
                    }
                ],
                "usage": {"input_tokens": 30, "output_tokens": 8, "total_tokens": 38},
            },
        )

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", send)
    return sent


SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": ["clarify"]},
        "target_role": {"anyOf": [{"type": "string"}, {"type": "null"}]},
    },
    "required": ["kind", "target_role"],
    "additionalProperties": False,
}


def test_llm_credentials_02_blind_zh_file_key_follows_inherited_base(tmp_path, monkeypatch):
    from corridor.cli.blind_zh import create_client

    env_file = _env(tmp_path, monkeypatch)
    sent = _capture(monkeypatch)
    try:
        client = create_client(
            planner="live",
            model="gpt-6.1-sol",
            config="A5",
            seed=1,
            user_policy="goal_bound",
            replay_dir=tmp_path / "replay",
            max_calls=None,
            max_usd=None,
            env_file=env_file,
        )
        client.complete(
            task_id="RT5-LLMCRED-02",
            step="blind-zh:0",
            sample_idx=1,
            prompt={"instruction": "Select an action.", "response_schema": SCHEMA},
            tool_state={"USER_REQUEST": "synthetic"},
            scripted=lambda: {"kind": "clarify", "target_role": None},
        )
    except (ValueError, RuntimeError):
        pass  # Refusing a mixed-source configuration is the safe outcome.
    # The named file pins both the key and https://api.openai.com/v1.
    assert all(host == "api.openai.com" for host, auth in sent if auth == f"Bearer {SECRET}"), (
        f"file credential sent to inherited host(s): {sorted({h for h, _ in sent})}"
    )


def test_llm_credentials_02b_named_env_options_same_redirect(tmp_path, monkeypatch):
    from corridor.agent.llm import LLMClient
    from corridor.bench.live_credentials import named_env_options

    env_file = _env(tmp_path, monkeypatch)
    sent = _capture(monkeypatch)
    try:
        client = LLMClient(
            mode="record",
            model="gpt-6.1-sol",
            replay_dir=tmp_path / "replay",
            **named_env_options("record", "gpt-6.1-sol", env_file),
        )
        client.complete(
            task_id="RT5-LLMCRED-02B",
            step="intent",
            prompt={"instruction": "Classify.", "response_schema": SCHEMA},
            tool_state={},
            scripted=lambda: {"kind": "clarify", "target_role": None},
        )
    except (ValueError, RuntimeError):
        pass
    assert all(host == "api.openai.com" for host, auth in sent if auth == f"Bearer {SECRET}"), (
        f"file credential sent to inherited host(s): {sorted({h for h, _ in sent})}"
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
