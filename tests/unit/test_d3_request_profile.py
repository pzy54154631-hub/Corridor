"""Actual transport parameters and recording identities must agree."""

import pytest

from corridor.agent.llm import LLMConfigurationError, ReplayMiss
from corridor.bench.recording import network_disabled, replay_client
from corridor.schemas.base import loads_exact
from tests.unit.test_llm_portability import client, complete, server


def test_chat_sampling_is_sent_recorded_and_replayed(tmp_path):
    with server() as mock:
        recorder = client(
            tmp_path,
            mock.base,
            seed=2,
            temperature=1,
            reasoning_effort="none",
            deployment_profile={"context_window": 4096, "engine": "synthetic-lock"},
        )
        observed = []
        recorder.transport_observers.append(observed.append)
        result = complete(recorder)
        body = mock.requests[0][1]
        assert body["seed"] == 2
        assert body["temperature"] == 1
        assert body["reasoning_effort"] == "none"
        assert observed == [body]
        saved = loads_exact(next(tmp_path.rglob("*.json")).read_bytes())
        assert saved["request"]["sampling"]["seed"] == 2
        assert saved["request"]["sampling"]["temperature"] == 1
        assert saved["request"]["deployment_profile"]["context_window"] == 4096
        replay = replay_client(recorder)
    with network_disabled():
        assert complete(replay).output == result.output
        replay.seed = 3
        with pytest.raises(ReplayMiss):
            complete(replay)


@pytest.mark.parametrize(
    "changes",
    [
        {"seed": True},
        {"seed": -1},
        {"temperature": 3},
        {"temperature": True},
        {"seed": 1, "api": "responses"},
    ],
)
def test_invalid_sampling_never_reaches_transport(tmp_path, changes):
    with pytest.raises(LLMConfigurationError):
        client(tmp_path, "http://127.0.0.1:8123/v1", **changes)


def test_locked_ollama_tag_is_an_allowed_fixed_model(tmp_path):
    recorder = client(tmp_path, "http://127.0.0.1:11434/v1", model="qwen3:8b-q4_K_M")
    assert recorder.model == "qwen3:8b-q4_K_M"


def test_colon_latest_is_not_a_fixed_model(tmp_path):
    with pytest.raises(LLMConfigurationError):
        client(tmp_path, "http://127.0.0.1:11434/v1", model="qwen3:latest")
