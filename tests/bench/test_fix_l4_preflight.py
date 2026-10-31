"""Synthetic FIX-L4 gates and local scheduling, with no blind corpus access."""

from itertools import groupby
from types import SimpleNamespace

import pytest

from corridor.bench import formal_gate, live_zh
from corridor.bench.bank_cases import write_json
from tests.bench.test_formal_gate import apparatus_fixture, probe_row, ready_probe


def test_workspace_checks_only_controlled_paths_against_head(tmp_path, monkeypatch):
    commands = []

    def git(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout=b"")

    monkeypatch.setattr(formal_gate.subprocess, "run", git)
    formal_gate.workspace_clean(tmp_path)
    assert len(commands) == 1
    paths = commands[0][commands[0].index("--") + 1 :]
    assert set(paths) >= {
        "src/",
        "tests/",
        "scripts/",
        "data/",
        "bench/",
        "prompts/",
        "pyproject.toml",
        "uv.lock",
    }
    assert not any(path.startswith(("docs/", "evidence/", "results/")) for path in paths)
    monkeypatch.setattr(
        formal_gate.subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(returncode=0, stdout=b" M src/changed.py\n"),
    )
    with pytest.raises(ValueError, match="controlled"):
        formal_gate.workspace_clean(tmp_path)


def test_formal_mini_requires_its_actual_probe_and_uses_its_profile(tmp_path, monkeypatch):
    from scripts import run_live_zh as runner

    probe = tmp_path / "probe/requests.json"
    mini = tmp_path / "mini/requests.json"
    write_json(probe, ready_probe())
    with pytest.raises(ValueError, match="missing"):
        formal_gate.load_run_probe(probe, mini)
    extra = {
        "providers": {
            "openai": {"gate_passed": True, "models": [probe_row("openai", "gpt-5.4-mini")]}
        }
    }
    write_json(mini, extra)
    replays = []
    monkeypatch.setattr(
        formal_gate, "verify_probe_replays", lambda *args, **kwargs: replays.append(args)
    )
    combined = formal_gate.load_run_probe(probe, mini)
    runner.require_global_probe_gate(combined)
    profile = runner._profile(
        {"provider": "openai", "model": "gpt-5.4-mini", "seed": 1}, [], combined
    )
    assert profile["model"] == "gpt-5.4-mini"
    assert profile["reasoning_effort"] == "low"
    assert replays[0][2] == mini.parent
    assert len(combined["providers"]["openai"]["models"]) == 2
    combined["providers"]["openai"]["models"].pop()
    with pytest.raises(ValueError, match="Mandatory"):
        runner.require_global_probe_gate(combined)


def test_same_local_model_is_contiguous_without_changing_block_identity():
    blocks = live_zh.build_plan([{"case_id": "synthetic", "harm_class": "synthetic"}])
    scheduled = live_zh.schedule_blocks(blocks)
    runs = [key for key, _ in groupby(scheduled, key=lambda row: (row["provider"], row["model"]))]
    local = [model for provider, model in runs if provider == "local"]
    assert len(local) == len(set(local)) == 3
    assert {row["block_id"] for row in scheduled} == {row["block_id"] for row in blocks}
    assert sorted(scheduled, key=lambda row: row["block_id"]) == sorted(
        blocks, key=lambda row: row["block_id"]
    )


def test_local_warmup_waits_for_locked_gpu_digest_and_times_out_safely():
    lock = [{"tier": "medium", "model": "qwen3:8b", "registry_manifest_sha256": "fixed"}]
    statuses = iter([
        {"models": []},
        {
            "models": [
                {"name": "qwen3:8b", "digest": "other", "size_vram": 1, "context_length": 32768}
            ]
        },
        {
            "models": [
                {"name": "qwen3:8b", "digest": "fixed", "size_vram": 1, "context_length": 32768}
            ]
        },
    ])
    now = [0]
    loads = []
    result = live_zh.ensure_local_runtime(
        lock,
        "qwen3-8b",
        load=lambda *args: loads.append(args),
        status=lambda: next(statuses),
        monotonic=lambda: now[0],
        sleep=lambda seconds: now.__setitem__(0, now[0] + seconds),
        timeout_seconds=3,
    )
    assert result["verified"] is True and len(loads) == 1
    with pytest.raises(ValueError, match="timeout"):
        live_zh.ensure_local_runtime(
            lock,
            "qwen3-8b",
            load=lambda *args: None,
            status=lambda: {"models": []},
            monotonic=lambda: now[0],
            sleep=lambda seconds: now.__setitem__(0, now[0] + seconds),
            timeout_seconds=2,
        )


def test_shortest_load_request_does_not_manage_service(monkeypatch):
    from scripts import run_live_zh as runner

    requests = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            return b"{}"

    monkeypatch.setattr(
        runner, "urlopen", lambda request, **kwargs: requests.append(request) or Response()
    )
    runner.load_local_model("qwen3:8b", 2)
    from corridor.schemas.base import loads_exact

    assert requests[0].full_url == "http://127.0.0.1:11434/api/generate"
    body = loads_exact(requests[0].data)
    assert body["model"] == "qwen3:8b" and body["prompt"] == ""
    assert body["stream"] is False and body["keep_alive"] != 0


@pytest.mark.parametrize("kind", ["sweep", "c2_blackbox"])
def test_apparatus_gate_rejects_missing_incomplete_or_nonzero_defects(tmp_path, monkeypatch, kind):
    from corridor.bench import apparatus

    source = tmp_path / "src/module.py"
    source.parent.mkdir()
    source.write_text("# synthetic\n", encoding="utf-8")
    hashes = {"src/module.py": formal_gate._digest(source)}
    monkeypatch.setattr(apparatus, "apparatus_source_hashes", lambda root: hashes)
    path = tmp_path / "summary.json"
    value = apparatus_fixture(tmp_path, path, kind, hashes)
    formal_gate.verify_apparatus(tmp_path, path, kind)
    for changed in (
        {"complete": False},
        {"apparatus_defects": 1},
        {"source_hashes": {}},
        {"passed": False},
        {"source_stable": False},
        {"observed_cells": 0},
        {"diagnostics_sha256": "0" * 64},
    ):
        write_json(path, {**value, **changed})
        with pytest.raises(ValueError):
            formal_gate.verify_apparatus(tmp_path, path, kind)
    write_json(path, value)
    source.write_text("# changed\n", encoding="utf-8")
    with pytest.raises(ValueError, match="digest"):
        formal_gate.verify_apparatus(tmp_path, path, kind)


@pytest.mark.parametrize("mutation", ["duplicate", "missing", "outcome_field", "defect"])
def test_apparatus_gate_rechecks_diagnostic_coverage_and_sanitization(
    tmp_path, monkeypatch, mutation
):
    from hashlib import sha256

    from corridor.bench import apparatus
    from corridor.crypto.jcs import jcs_bytes
    from corridor.schemas.base import loads_exact

    source = tmp_path / "source.py"
    source.write_text("# synthetic\n", encoding="utf-8")
    hashes = {"source.py": formal_gate._digest(source)}
    monkeypatch.setattr(apparatus, "apparatus_source_hashes", lambda root: hashes)
    summary_path = tmp_path / "summary.json"
    summary = apparatus_fixture(tmp_path, summary_path, "c2_blackbox", hashes)
    diagnostics = tmp_path / "diagnostics.jsonl"
    rows = [loads_exact(line) for line in diagnostics.read_bytes().splitlines()]
    if mutation == "duplicate":
        rows[-1] = rows[0]
    elif mutation == "missing":
        rows.pop()
    elif mutation == "outcome_field":
        rows[0]["completed"] = True
    else:
        rows[0].update(status="apparatus_defect", error_class="apparatus_defect")
    raw = b"".join(jcs_bytes(row) + b"\n" for row in rows)
    diagnostics.write_bytes(raw)
    summary["diagnostics_sha256"] = sha256(raw).hexdigest()
    write_json(summary_path, summary)
    with pytest.raises(ValueError, match="apparatus"):
        formal_gate.verify_apparatus(tmp_path, summary_path, "c2_blackbox")


@pytest.mark.parametrize(
    "mutation",
    ["manifest_drift", "unregistered_corpus", "unstable", "missing_binding", "alternate_cases"],
)
def test_blackbox_gate_binds_only_canonical_frozen_manifest(tmp_path, monkeypatch, mutation):
    from corridor.bench import apparatus

    source = tmp_path / "source.py"
    source.write_text("# synthetic\n", encoding="utf-8")
    hashes = {"source.py": formal_gate._digest(source)}
    monkeypatch.setattr(apparatus, "apparatus_source_hashes", lambda root: hashes)
    path = tmp_path / "summary.json"
    summary = apparatus_fixture(tmp_path, path, "c2_blackbox", hashes)
    formal_gate.verify_apparatus(tmp_path, path, "c2_blackbox")
    kwargs = {}
    if mutation == "manifest_drift":
        (tmp_path / "data/blind-zh/FROZEN-SHA256.txt").write_bytes(b"changed synthetic manifest")
    elif mutation == "unregistered_corpus":
        summary["input_binding"]["corpus_identity"] = "unregistered"
    elif mutation == "unstable":
        summary["input_stable"] = False
    elif mutation == "missing_binding":
        del summary["input_binding"]
    else:
        kwargs["cases"] = tmp_path / "other-synthetic-cases"
    write_json(path, summary)
    with pytest.raises(ValueError, match="corpus|manifest|input binding"):
        formal_gate.verify_apparatus(tmp_path, path, "c2_blackbox", **kwargs)
