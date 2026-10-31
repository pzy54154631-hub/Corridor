"""Truncation evidence survives worker aggregation and historical report formats."""

from fractions import Fraction
from types import SimpleNamespace

import pytest

from corridor.agent.llm import is_output_truncated, response_diagnostics
from corridor.schemas.base import loads_exact
from scripts import run_live_zh
from scripts.final_report_zh import finish_reason_length


def _worker_result(tmp_path, monkeypatch, calls):
    client = SimpleNamespace(calls=calls, spent_usd=Fraction(0))
    monkeypatch.setattr(run_live_zh, "make_worker_client", lambda *args, **kwargs: client)
    monkeypatch.setattr(run_live_zh, "load_cases", lambda path: [{"case_id": "synthetic"}])
    monkeypatch.setattr(
        run_live_zh,
        "run_case",
        lambda *args, **kwargs: {
            "status": "runtime_error",
            "completed": False,
            "replay_control": {"model_failures": [{"diagnostics": call} for call in calls]},
        },
    )
    spec = {
        "row": {
            "experiment": "E1",
            "case_id": "synthetic",
            "config": "A5",
            "seed": 1,
            "variant": "attack",
        },
        "currency": "LOCAL",
        "episode_cap": "2",
        "cases": str(tmp_path),
    }
    output = tmp_path / "result.json"
    run_live_zh._worker(spec, output, "record", env_file=None)
    return loads_exact(output.read_bytes())


@pytest.mark.parametrize(
    "diagnostics",
    [
        {"output_truncated": True},
        {"finish_reason": "length", "output_tokens": 511},
        {"incomplete_details": {"reason": "max_output_tokens"}, "output_tokens": 511},
        {"response_shape": {"finish_reason": "length"}},
        {"output_tokens": 512},
        {"usage": {"output_tokens": 512}},
        {"recorded_usage": {"output_tokens": 512}},
    ],
)
def test_truncation_formats_agree_in_worker_and_report(tmp_path, monkeypatch, diagnostics):
    assert is_output_truncated(diagnostics)
    retained = {"replay_control": {"model_failures": [{"diagnostics": diagnostics}]}}
    assert finish_reason_length([retained]) == 1
    result = _worker_result(tmp_path, monkeypatch, [diagnostics])
    assert result["usage_measurements"]["finish_reason_length"] == 1
    # Measured usage and retained failure metadata describe the same call.
    assert finish_reason_length([result]) == 1


@pytest.mark.parametrize(
    "diagnostics",
    [
        {},
        {"output_tokens": 511},
        {"finish_reason": "stop"},
        {"incomplete_details": {"reason": "content_filter"}},
        {"output_truncated": False},
        {"output_truncated": "true"},
        {"output_tokens": "512"},
        {"response_shape": None, "incomplete_details": None, "usage": None},
    ],
)
def test_non_truncation_metadata_does_not_create_events(diagnostics):
    assert not is_output_truncated(diagnostics)
    retained = {"replay_control": {"model_failures": [{"diagnostics": diagnostics}]}}
    assert finish_reason_length([retained]) == 0


def test_summed_multicall_usage_at_cap_does_not_imply_truncation(tmp_path, monkeypatch):
    calls = [{"output_tokens": 256}, {"output_tokens": 256}]
    result = _worker_result(tmp_path, monkeypatch, calls)
    assert result["usage_measurements"]["output_tokens"] == 512
    assert result["usage_measurements"]["model_requests"] == 2
    assert result["usage_measurements"]["finish_reason_length"] == 0
    assert finish_reason_length([result]) == 0
    assert (
        finish_reason_length([{"usage_measurements": {"model_requests": 2, "output_tokens": 512}}])
        == 0
    )
    assert (
        finish_reason_length([{"usage_measurements": {"model_requests": 1, "output_tokens": 512}}])
        == 1
    )


def test_responses_incomplete_before_exact_token_cap_is_reported():
    diagnostics = response_diagnostics(
        {
            "status": "incomplete",
            "incomplete_details": {"reason": "max_output_tokens"},
            "output": [],
            "usage": {"output_tokens": 511},
        },
        max_output_tokens=512,
    )
    assert diagnostics["output_truncated"] is True
    assert (
        finish_reason_length([
            {"replay_control": {"model_failures": [{"diagnostics": diagnostics}]}}
        ])
        == 1
    )


def test_truncation_helper_respects_call_specific_output_cap():
    assert is_output_truncated({"output_tokens": 128}, max_output_tokens=128)
    assert not is_output_truncated({"output_tokens": 128})
