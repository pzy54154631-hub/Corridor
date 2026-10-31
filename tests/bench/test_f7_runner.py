"""Freeze-7 credentials and plan selection cannot cross provider boundaries."""

from argparse import Namespace

import httpx
import pytest

from corridor.bench.bank_cases import write_json
from corridor.schemas.base import loads_exact
from scripts import run_live_zh as runner


def credential(tmp_path, provider, base, key):
    path = tmp_path / (provider + ".env")
    path.write_text(
        f"LLM_PROVIDER={provider}\nLLM_API_BASE={base}\nLLM_API_KEY={key}\n"
        "LLM_MODEL=deepseek-chat\n",
        encoding="utf-8",
    )
    return path


def spec(tmp_path, provider, base):
    return {
        "row": {"provider": provider, "experiment": "E1", "config": "A0"},
        "replay_dir": str(tmp_path / provider / "recording"),
        "episode_cap": "15",
        "client_profile": {
            "provider": provider,
            "api_base": base,
            "model": "deepseek-chat",
            "api": "chat",
            "seed": None,
            "reasoning_effort": None,
            "assumed_price_in": "2",
            "assumed_price_out": "3",
        },
    }


def test_worker_uses_only_bound_provider_key_on_actual_wire(tmp_path, monkeypatch):
    openai = credential(tmp_path, "openai", "https://api.openai.com/v1", "FAKE_OPENAI")
    domestic = credential(tmp_path, "deepseek", "https://api.deepseek.com", "FAKE_DEEPSEEK")
    sent = []

    def transport(_self, request):
        sent.append((str(request.url), request.headers["authorization"]))
        return httpx.Response(
            200,
            json={
                "id": "mock",
                "model": "deepseek-chat",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": '{"handle":"OK"}'},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 4, "completion_tokens": 4, "total_tokens": 8},
            },
        )

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", transport)
    client = runner.make_worker_client(
        spec(tmp_path, "deepseek", "https://api.deepseek.com"),
        "record",
        env_file=openai,
        provider_env={"deepseek": domestic},
    )
    client.complete(
        task_id="F7-MOCK",
        step="probe",
        prompt={"instruction": "Reply JSON"},
        tool_state={"handle": "OK"},
        scripted=lambda: {"handle": "OK"},
    )
    assert sent == [("https://api.deepseek.com/chat/completions", "Bearer FAKE_DEEPSEEK")]


def test_worker_never_uses_openai_env_as_domestic_fallback(tmp_path):
    openai = credential(tmp_path, "openai", "https://api.openai.com/v1", "FAKE_OPENAI")
    with pytest.raises(ValueError, match="missing_credentials"):
        runner.make_worker_client(
            spec(tmp_path, "deepseek", "https://api.deepseek.com"), "record", env_file=openai
        )


def test_worker_rejects_mismatched_provider_before_client(tmp_path, monkeypatch):
    openai = credential(tmp_path, "openai", "https://api.openai.com/v1", "FAKE_OPENAI")
    monkeypatch.setattr(runner, "LLMClient", lambda **kw: pytest.fail("client constructed"))
    with pytest.raises(ValueError):
        runner.make_worker_client(
            spec(tmp_path, "deepseek", "https://api.deepseek.com"),
            "record",
            provider_env={"deepseek": openai},
        )


def test_provider_env_rejects_duplicates_openai_and_invalid_entries():
    for entries in [["openai=x"], ["deepseek=x", "deepseek=y"], ["deepseek"], ["unknown=x"]]:
        with pytest.raises(ValueError):
            runner.provider_env_paths(entries)


def test_plan_selection_and_source_are_frozen_identity(tmp_path):
    path = tmp_path / "plan.json"
    original = {"blocks": [], "rows": [], "row_set": "domestic-api", "source_plan_sha256": None}
    runner.retain_plan(path, original)
    runner.retain_plan(path, original)
    for changed in [{"row_set": "all"}, {"source_plan_sha256": "a" * 64}]:
        with pytest.raises(ValueError, match="frozen run identity"):
            runner.retain_plan(path, {**original, **changed})
    assert loads_exact(path.read_bytes()) == original


def test_empty_continuation_exits_without_blind_access_or_gates(tmp_path, monkeypatch, capsys):
    source = tmp_path / "source"
    blocks = [
        block
        for block in runner.build_plan([{"case_id": "synthetic", "harm_class": "synthetic"}])
        if block["priority"] == 2
    ]
    write_json(
        source / "plan.json",
        {"blocks": blocks, "rows": [row for block in blocks for row in runner.planned_rows(block)]},
    )
    for block in blocks:
        write_json(
            source / "blocks" / (block["block_id"] + ".json"),
            {
                "rows": runner.planned_rows(block),
                "reason": "missing_credentials",
            },
        )
    monkeypatch.setattr(runner, "load_cases", lambda *a: pytest.fail("case read"))
    monkeypatch.setattr(runner, "run_formal_gate", lambda **kw: pytest.fail("gate run"))
    args = Namespace(
        output=tmp_path / "output",
        continuation_of=source,
        row_set="cut-continuation",
        episode_cap="2",
        execute=True,
        domestic_api=[],
        cases=tmp_path / "unopened",
    )
    runner.coordinate(args)
    assert "No budget-cutoff blocks" in capsys.readouterr().out
