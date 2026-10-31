"""A complete bank task through a real local HTTP protocol and offline replay."""

import os

import pytest

from corridor.agent.llm import LLMClient
from corridor.bench.compatibility_mock import LocalCompatibilityMock
from corridor.bench.recording import network_disabled, observation_hash, replay_client
from corridor.cli.judge import run_bank_task


@pytest.mark.parametrize(
    "model,provider", [("deepseek-chat", "deepseek"), ("qwen-plus", "dashscope")]
)
def test_complete_j01_matches_scripted_with_local_http_and_strict_replay(
    tmp_path, monkeypatch, model, provider
):
    for name in tuple(os.environ):
        if name.startswith("LLM_"):
            monkeypatch.delenv(name)
    scripted = run_bank_task("J01", LLMClient(env_file=None), tmp_path / "scripted")
    with LocalCompatibilityMock() as mock:
        recorder = mock.client(model, provider, tmp_path / "replay")
        recorded = run_bank_task("J01", recorder, tmp_path / "record")
        assert mock.request_count == recorder.api_calls > 0
        assert all(call["source"] == "local-mock-recorded" for call in recorder.calls)
        strict = replay_client(recorder)
    with network_disabled():
        repeated = run_bank_task("J01", strict, tmp_path / "strict")
    assert scripted["passed"] and recorded["passed"] and repeated["passed"]
    assert observation_hash(scripted) == observation_hash(recorded) == observation_hash(repeated)
