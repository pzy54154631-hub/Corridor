"""llm-credentials-03: E2 repeat seeds share one recording identity.

PREREG-DOMESTIC: live cases repeat n=3 "with different seeds", every call is
recorded and strictly replayable, and R2-11 requires the seed to be "written into
the recording identity".  The standalone live entry point
`corridor interact-eval --planner live --env-file ...` (FIX-L1) loops seeds 1..3
into one shared replay directory, but for the non-common E2 cases and J tasks
`domestic_session` / `judge_runner` call `LLMClient.complete` with
`task_id=case_id` and the default `sample_idx=0`, and `named_env_options` adds no
`deployment_profile.repeat_seed`.  The seed therefore never reaches the replay key:

* record: repeat 2 and 3 hit "Refusing to replace an existing live replay" and
  become `runtime_error` (1/3 valid per case);
* strict: all three "repetitions" replay repeat 1's single recording, so strict
  replay reports 3/3 valid repeats that were never observed live.

Only the common immediate-transfer subset (blind_zh.run_case: sample_idx=seed)
gets distinct identities.  No network: a fake provider behind the patched httpx
transport answers exactly what the scripted planner would.
"""

import os

import httpx
import pytest
from typer.testing import CliRunner

from corridor.agent import llm
from corridor.cli import app
from corridor.crypto.jcs import jcs_bytes
from corridor.schemas.base import loads_exact

SECRET = "SYNTHETIC_RT5_E2_REPEAT_CREDENTIAL_0003"
CASE = "ZH-BI-01"  # non-common E2 scenario that consults the model


def _fake_provider(monkeypatch):
    pending = []
    original = llm.LLMClient.complete

    def complete(self, **request):
        pending.append(request["scripted"])
        try:
            return original(self, **request)
        finally:
            pending.pop()

    def send(_self, request):
        body = loads_exact(request.content)
        answer = jcs_bytes(pending[-1]()).decode("utf-8")
        item = (
            {"type": "function_call", "name": "emit_result", "arguments": answer}
            if "tools" in body
            else {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": answer}],
            }
        )
        return httpx.Response(
            200,
            json={
                "model": body["model"],
                "status": "completed",
                "output": [item],
                "usage": {"input_tokens": 30, "output_tokens": 8, "total_tokens": 38},
            },
        )

    monkeypatch.setattr(llm.LLMClient, "complete", complete)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", send)


def _invoke(output, planner, env_file):
    return CliRunner().invoke(
        app,
        [
            "interact-eval",
            "--planner",
            planner,
            "--model",
            "gpt-6.1-sol",
            "--cases",
            CASE,
            "--env-file",
            str(env_file),
            "--output",
            str(output),
        ],
    )


def test_llm_credentials_03_e2_repeats_are_distinct_recordings(tmp_path, monkeypatch):
    for name in tuple(os.environ):
        if name.startswith("LLM_"):
            monkeypatch.delenv(name)
    env_file = tmp_path / "credentials.env"
    env_file.write_text(
        f"OPENAI_API_KEY={SECRET}\nLLM_MAX_CALLS=20\nLLM_MAX_USD=10\n", encoding="utf-8"
    )
    _fake_provider(monkeypatch)
    output = tmp_path / "interaction"
    recorded = _invoke(output, "live", env_file)
    assert recorded.exit_code == 0, recorded.output
    live_rows = loads_exact((output / "results.json").read_bytes())["results"]
    identities = [
        tuple(call["request_hash"] for call in row["model_call_observations"]) for row in live_rows
    ]
    statuses = [(row["seed"], row["status"], row.get("error_type")) for row in live_rows]
    assert all(identities), "the case must consult the model in every repeat"
    # Fixed behaviour: three independently recorded repeats with distinct identities.
    assert len(set(identities)) == 3, f"repeat seeds share a replay identity: {statuses}"
    assert [status for _, status, _ in statuses] == ["valid_completed"] * 3, statuses


def test_llm_credentials_03b_strict_replay_reuses_repeat_one_for_all_seeds(tmp_path, monkeypatch):
    """Consequence in strict mode: repeat 1's recording stands in for repeats 2 and 3."""
    for name in tuple(os.environ):
        if name.startswith("LLM_"):
            monkeypatch.delenv(name)
    env_file = tmp_path / "credentials.env"
    env_file.write_text(
        f"OPENAI_API_KEY={SECRET}\nLLM_MAX_CALLS=20\nLLM_MAX_USD=10\n", encoding="utf-8"
    )
    _fake_provider(monkeypatch)
    output = tmp_path / "interaction"
    assert _invoke(output, "live", env_file).exit_code == 0
    live = loads_exact((output / "results.json").read_bytes())["results"]
    monkeypatch.undo()  # strict replay: no provider, no transport
    for name in tuple(os.environ):
        if name.startswith("LLM_"):
            monkeypatch.delenv(name)
    strict_output = tmp_path / "interaction"
    replayed = _invoke(strict_output, "strict", tmp_path / "unused.env")
    assert replayed.exit_code == 0, replayed.output
    strict = loads_exact((strict_output / "results.json").read_bytes())["results"]
    live_valid = sum(row["status"] == "valid_completed" for row in live)
    strict_valid = sum(row["status"] == "valid_completed" for row in strict)
    # Strict replay must reproduce the recorded run, not manufacture extra repeats.
    assert strict_valid == live_valid, (
        f"live valid repeats {live_valid}/3 but strict replay reports {strict_valid}/3"
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
