"""Exercise the actual coordinator with mocked gates and a zero-network worker."""

import subprocess
import sys
from argparse import Namespace
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace

import pytest

from corridor.bench.bank_cases import write_json
from corridor.schemas.base import loads_exact


@pytest.fixture
def mock_coordinator(tmp_path, monkeypatch):
    from scripts import run_live_zh as runner

    root = tmp_path / "repository"
    (root / "docs").mkdir(parents=True)
    (root / "docs/PREREG-DOMESTIC.md").write_text("mock prereg", encoding="utf-8")
    (root / ".env").write_text(
        "LLM_PROVIDER=openai\nLLM_API_BASE=https://api.openai.com/v1\nOPENAI_API_KEY=synthetic-test-key\n",
        encoding="utf-8",
    )
    write_json(root / "evidence/probe/requests.json", {"providers": {}})
    write_json(root / "local/models-lock.json", [])
    cases = [{"case_id": f"sample-{index}", "harm_class": "synthetic"} for index in range(2)]
    plan = runner.build_plan(cases)
    main = {**next(block for block in plan if block["priority"] == 2), "configs": ["A0", "A5"]}
    independent = next(
        block for block in plan if block["experiment"] == "E2" and block["provider"] == "local"
    )
    monkeypatch.setattr(runner, "ROOT", root)
    monkeypatch.setattr(runner, "LOCAL_ROOT", root / "local")
    monkeypatch.setattr(runner, "load_cases", lambda path: cases)
    monkeypatch.setattr(runner, "build_plan", lambda *a, **kw: [main, independent])
    monkeypatch.setattr(runner, "run_formal_gate", lambda **kw: {"ready": True})
    monkeypatch.setattr(runner, "require_global_probe_gate", lambda probe: None)
    monkeypatch.setattr(
        runner,
        "load_run_probe",
        lambda *a: {
            "providers": {
                "qwen-plus": {"api_base": "https://dashscope.aliyuncs.com/compatible-mode/v1"}
            }
        },
    )
    monkeypatch.setattr(runner, "workspace_clean", lambda *a: None)
    monkeypatch.setattr(runner, "ensure_local_runtime", lambda *a, **kw: {"verified": True})
    monkeypatch.setattr(runner, "verify_local_lock", lambda *a: {"verified": True})
    monkeypatch.setattr(runner, "local_status", lambda: {})
    monkeypatch.setattr(runner, "local_running_models", lambda: {})
    monkeypatch.setattr(runner, "verify_local_runtime", lambda *a: {"verified": True})
    monkeypatch.setattr(
        runner,
        "_profile",
        lambda row, *a: {
            "model": row["model"],
            "provider": row["provider"],
            "api_base": "https://api.openai.com/v1"
            if row["provider"] == "openai"
            else "https://dashscope.aliyuncs.com/compatible-mode/v1",
        },
    )
    monkeypatch.setattr(
        runner.subprocess,
        "check_output",
        lambda command, **kw: "domestic-freeze-3" if "describe" in command else "",
    )
    state = SimpleNamespace(recorded=[], replayed=[], hook=lambda *a: None, main=main)

    def process(command, **kwargs):
        if "--worker" not in command:
            return SimpleNamespace(returncode=0)
        spec_path = Path(command[command.index("--worker") + 1])
        spec = loads_exact(spec_path.read_bytes())
        output = Path(command[command.index("--output") + 1])
        strict = "--strict" in command
        identity = (spec["row"]["experiment"], spec["row"]["config"], spec_path.parent.name)
        (state.replayed if strict else state.recorded).append(identity)
        override = state.hook(spec, output, strict)
        if override is not None:
            return override
        result = (
            loads_exact(Path(spec["result_path"]).read_bytes())
            if strict
            else {
                **spec["row"],
                "status": "valid_completed",
                "completed": True,
                "prohibited_effect": False,
                "usage_measurements": {"cost": "1/100", "total_tokens": 100},
            }
        )
        write_json(output, result)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(runner.subprocess, "run", process)
    state.runner = runner
    state.args = Namespace(
        cases=root / "synthetic-cases",
        output=tmp_path / "output",
        domestic_api=None,
        episode_cap="2",
        execute=True,
        stages="",
    )
    return state


@pytest.mark.parametrize("http_status", [401, 403])
def test_rerun_authentication_defect_seals_block_and_stops_before_next(
    mock_coordinator, monkeypatch, tmp_path, http_status
):
    from corridor.agent.llm import LLMOutputError
    from corridor.bench.episode_control import error_class
    from corridor.bench.live_zh import planned_rows
    from corridor.bench.rerun_zh import create_manifest
    from corridor.crypto.jcs import H

    state = mock_coordinator
    runner = state.runner
    cases = [{"case_id": f"sample-{index}", "harm_class": "synthetic"} for index in range(2)]
    blocks = runner.build_pilot_plan(cases)
    source = tmp_path / "source" / "plan.json"
    write_json(
        source,
        {
            "blocks": blocks,
            "rows": [row for block in blocks for row in planned_rows(block)],
            "episode_cap": "2",
            "case_hash": H(cases),
        },
    )
    for block in blocks:
        write_json(
            source.parent / "blocks" / (block["block_id"] + ".json"),
            {
                "block_id": block["block_id"],
                "rows": planned_rows(block),
            },
        )
    manifest = tmp_path / "rerun-manifest.json"
    write_json(
        manifest,
        create_manifest(source, "LIVE-synthetic", "auth-stop", [b["block_id"] for b in blocks]),
    )
    proof = tmp_path / "synthetic-proof.json"
    write_json(proof, {"synthetic": True})
    monkeypatch.setattr(
        "corridor.bench.rerun_proof.verify_bundle",
        lambda *a, **kw: {
            "bundle_path": str(proof),
            "bundle_sha256": "a" * 64,
        },
    )
    monkeypatch.setattr(runner, "replay_code_hashes", lambda: {"synthetic.py": "a" * 64})
    state.args.row_set = "rerun"
    state.args.rerun_manifest = manifest
    state.args.rerun_proof = proof

    def hook(spec, output, strict):
        if not strict:
            write_json(
                output,
                {
                    **spec["row"],
                    "status": "runtime_error",
                    "completed": False,
                    "error_class": error_class(
                        LLMOutputError("Synthetic authentication failure"),
                        {
                            "http_status": http_status,
                        },
                    ),
                },
            )
            return SimpleNamespace(returncode=0)

    state.hook = hook
    with pytest.raises(ValueError, match="R13 apparatus defect stop"):
        runner.coordinate(state.args)
    sealed = loads_exact(
        (state.args.output / "blocks" / (blocks[0]["block_id"] + ".json")).read_bytes()
    )
    assert sealed["replay_passed"] is True
    assert len(sealed["attempts"]) == len(blocks[0]["configs"]) == 2
    assert all(
        row["error_class"] == "apparatus_defect" and row["attempt"] == 0
        for row in sealed["attempts"]
    )
    assert len(state.recorded) == len(state.replayed) == 2
    assert not (state.args.output / "blocks" / (blocks[1]["block_id"] + ".json")).exists()
    assert not (state.args.output / "episodes" / blocks[1]["block_id"]).exists()


@pytest.mark.parametrize("credential_file", ["missing", "empty", "unrelated"])
def test_missing_credentials_skip_paid_blocks_before_admission_or_spend(
    mock_coordinator, monkeypatch, credential_file
):
    state = mock_coordinator
    selected = state.runner.ROOT / "explicit-credentials.env"
    if credential_file != "missing":
        selected.write_text(
            "OPENAI_API_KEY=\n" if credential_file == "empty" else "UNRELATED=synthetic\n",
            encoding="utf-8",
        )
    state.args.env_file = selected
    monkeypatch.setenv("OPENAI_API_KEY", "inherited-value-must-not-enable-paid-calls")
    state.runner.coordinate(state.args)
    assert {row[0] for row in state.recorded} == {"E2"}
    plan = loads_exact((state.args.output / "plan.json").read_bytes())
    for block in plan["blocks"]:
        if block["provider"] == "local":
            continue
        report = loads_exact(
            (state.args.output / "blocks" / f"{block['block_id']}.json").read_bytes()
        )
        assert all(row["status"] == "not_run" for row in report["rows"])
        assert all(row["reason"] == "missing_credentials" for row in report["rows"])
    assert not list(state.args.output.glob("spending-*.jsonl"))
    assert not list(state.args.output.glob("budget-*.jsonl"))
    assert not (state.args.output / "admissions").exists()


def test_missing_credentials_preserve_existing_attempted_failure(mock_coordinator):
    state = mock_coordinator

    def hook(spec, output, strict):
        if not strict and spec["row"]["experiment"] == "E1":
            write_json(
                output,
                {**spec["row"], "status": "runtime_error", "error_class": "apparatus_defect"},
            )
            return SimpleNamespace(returncode=0)

    state.hook = hook
    state.runner.coordinate(state.args)
    path = state.args.output / "blocks" / f"{state.main['block_id']}.json"
    previous = path.read_bytes()
    before = list(state.recorded)
    state.args.env_file = state.runner.ROOT / "missing.env"
    state.runner.coordinate(state.args)
    assert path.read_bytes() == previous
    assert state.recorded == before


def test_failed_pilot_replay_blocks_paid_dependents_but_not_local(mock_coordinator):
    state = mock_coordinator

    def hook(spec, output, strict):
        if strict and spec["row"]["experiment"] == "pilot":
            return SimpleNamespace(returncode=1)

    state.hook = hook
    state.runner.coordinate(state.args)
    estimate = loads_exact((state.args.output / "pilot-estimate.json").read_bytes())
    assert estimate["passed"] is False
    assert not any(row[0] == "E1" for row in state.recorded)
    assert any(row[0] == "E2" for row in state.recorded)


def test_local_residency_timeout_keeps_every_unstarted_row_and_reason(
    mock_coordinator, monkeypatch
):
    state = mock_coordinator

    def unavailable(*args, **kwargs):
        raise ValueError("Local model residency timeout: locked model is not resident on GPU")

    monkeypatch.setattr(state.runner, "ensure_local_runtime", unavailable)
    state.runner.coordinate(state.args)
    assert not any(row[0] == "E2" for row in state.recorded)
    blocks = state.runner.build_plan([])
    local = next(block for block in blocks if block["provider"] == "local")
    report = loads_exact((state.args.output / "blocks" / f"{local['block_id']}.json").read_bytes())
    assert len(report["rows"]) == len(local["configs"])
    assert all(row["status"] == "not_run" for row in report["rows"])
    assert all(row["reason"] == report["reason"] for row in report["rows"])
    assert all(row["error_class"] == "local_resource" for row in report["rows"])


def test_fix_l3_budget_cutoff_preserves_complete_pairs_and_all_trailing_blocks(
    mock_coordinator, monkeypatch
):
    state = mock_coordinator
    from corridor.bench.live_zh import build_plan

    full = build_plan([{"case_id": name} for name in ("a", "b", "c")])
    blocks = [
        {**block, "configs": ["A0", "A5"] if block["case_id"] != "c" else ["A0"]}
        for block in full
        if block["priority"] == 2
        and block["seed"] == 1
        and (block["case_id"] != "c" or block["variant"] == "attack")
    ]
    blocks = [
        {**block, "experiment": "E2"} if block["case_id"] == "c" else block for block in blocks
    ]
    monkeypatch.setattr(state.runner, "build_plan", lambda *a, **kw: blocks)
    monkeypatch.setattr(
        state.runner,
        "BUDGET_CAPS",
        {"USD": Fraction(1, 10), "CNY": Fraction(50), "LOCAL": Fraction(0)},
    )
    state.runner.coordinate(state.args)
    results = [
        loads_exact((state.args.output / "blocks" / (block["block_id"] + ".json")).read_bytes())
        for block in blocks
    ]
    assert all(
        row["status"] == "valid_completed" for result in results[:2] for row in result["rows"]
    )
    assert all(row["status"] == "not_run" for result in results[2:] for row in result["rows"])
    assert len([row for row in state.recorded if row[0] == "E1"]) == 4
    before = list(state.recorded)
    state.args.stages = "E2"
    state.runner.coordinate(state.args)
    assert state.recorded == before


def test_corrupt_first_replay_observation_does_not_skip_later_attempts(mock_coordinator):
    state = mock_coordinator

    def hook(spec, output, strict):
        if not strict and spec["row"]["experiment"] == "E1" and spec["row"]["config"] == "A5":
            first = output.parents[2] / "A0/0/result.json"
            first.write_bytes(b"invalid-json")

    state.hook = hook
    state.runner.coordinate(state.args)
    result = loads_exact(
        (state.args.output / "blocks" / (state.main["block_id"] + ".json")).read_bytes()
    )
    assert result["replay_passed"] is False
    assert ("E1", "A5", "0") in state.replayed
    assert any(row[0] == "E2" for row in state.recorded)


@pytest.mark.parametrize("failure", ["timeout", "crash"])
def test_worker_timeout_and_crash_are_saved_and_later_blocks_continue(mock_coordinator, failure):
    state = mock_coordinator

    def hook(spec, output, strict):
        if not strict and spec["row"]["experiment"] == "E1" and spec["row"]["config"] == "A0":
            if failure == "timeout":
                raise subprocess.TimeoutExpired("synthetic-worker", 600)
            return SimpleNamespace(returncode=1)

    state.hook = hook
    state.runner.coordinate(state.args)
    folder = state.args.output / "episodes" / state.main["block_id"] / "A0/0"
    result = loads_exact((folder / "result.json").read_bytes())
    assert result["status"] == "runtime_error"
    assert result["reason"] == ("worker_timeout" if failure == "timeout" else "worker_crash")
    assert ("E1", "A0", "0") in state.replayed
    assert any(row[0] == "E2" for row in state.recorded)
    before = list(state.recorded)
    state.runner.coordinate(state.args)
    assert state.recorded == before


def test_stage_resume_preserves_plan_and_does_not_execute_unselected_blocks(mock_coordinator):
    state = mock_coordinator
    state.args.stages = "E2"
    state.runner.coordinate(state.args)
    assert {row[0] for row in state.recorded} == {"pilot", "E2"}
    assert not (state.args.output / "blocks" / (state.main["block_id"] + ".json")).exists()
    before = list(state.recorded)
    plan = (state.args.output / "plan.json").read_bytes()
    state.args.stages = "E1"
    state.runner.coordinate(state.args)
    assert state.recorded[: len(before)] == before
    assert {row[0] for row in state.recorded[len(before) :]} == {"E1"}
    assert (state.args.output / "plan.json").read_bytes() == plan


def test_each_retry_replays_its_own_attempt_directory_and_resume_skips_calls(mock_coordinator):
    state = mock_coordinator

    def hook(spec, output, strict):
        if (
            not strict
            and spec["row"]["experiment"] == "E1"
            and spec["row"]["config"] == "A0"
            and output.parent.name == "0"
        ):
            write_json(
                output,
                {
                    **spec["row"],
                    "status": "runtime_error",
                    "error_class": "network_quota",
                    "usage_measurements": {"cost": "1/100", "total_tokens": 100},
                },
            )
            return SimpleNamespace(returncode=0)

    state.hook = hook
    state.runner.coordinate(state.args)
    assert [row for row in state.replayed if row[0] == "E1"] == [
        ("E1", "A0", "0"),
        ("E1", "A0", "1"),
        ("E1", "A5", "0"),
    ]
    before = list(state.recorded)
    state.runner.coordinate(state.args)
    assert state.recorded == before


def test_interrupted_worker_checkpoint_is_retained_without_claiming_full_replay(
    tmp_path,
):
    from scripts.run_live_zh import record_worker_interruption

    folder = tmp_path / "attempt"
    control = {"checkpoints": [{"label": "tool.before"}], "terminal": None}
    write_json(folder / "episode/control.json", control)
    write_json(folder / "result.json", {"unfinished_observation": "retained"})
    original = (folder / "result.json").read_bytes()
    result = record_worker_interruption(folder, {"config": "A0"}, "worker_crash")
    assert (folder / "interrupted-worker-result.json").read_bytes() == original
    assert loads_exact((folder / "episode/control.json").read_bytes()) == control
    assert result["interrupted_prefix"] is True
    assert result["prohibited_effect"] is None
    assert result["replay_control"]["terminal"]["position"] == 1


def test_zero_shared_allowance_truncates_and_replays_before_any_client_or_case_read(
    tmp_path, monkeypatch
):
    from corridor.bench.live_zh import execute_block, replay_projection
    from scripts import run_live_zh as runner

    def forbidden(*args, **kwargs):
        pytest.fail("Exhausted shared allowance must not initialize a client or read cases")

    monkeypatch.setattr(runner, "make_worker_client", forbidden)
    monkeypatch.setattr(runner, "load_cases", forbidden)
    block = {
        "block_id": "synthetic-budget-zero",
        "case_id": "synthetic-case",
        "seed": 1,
        "configs": ["A0"],
    }
    calls = []

    def run(row, attempt):
        calls.append(attempt)
        spec = {"row": row, "currency": "USD", "episode_cap": "0"}
        live, strict = tmp_path / "live.json", tmp_path / "strict.json"
        runner.worker(spec, live, "record")
        runner.worker({**spec, "result_path": str(live)}, strict, "strict")
        observed = loads_exact(live.read_bytes())
        assert replay_projection(observed) == replay_projection(loads_exact(strict.read_bytes()))
        assert observed["usage_measurements"]["cost"] == "0"
        return observed

    result = execute_block(block, run, lambda rows: True)
    assert calls == [0]
    assert result["block_passed"] is True
    assert result["rows"][0]["status"] == "truncated"
    assert result["rows"][0]["truncation_limit"] == "shared_budget"


def test_provider_overrun_stops_next_paid_client_but_local_block_continues(mock_coordinator):
    state = mock_coordinator
    paid_client_calls = []

    def hook(spec, output, strict):
        if strict or spec["row"]["experiment"] != "E1":
            return None
        if spec["row"]["config"] == "A0":
            paid_client_calls.append("A0")
            write_json(
                output,
                {
                    **spec["row"],
                    "status": "valid_completed",
                    "usage_measurements": {"cost": "3", "total_tokens": 100},
                },
            )
        else:
            assert spec["episode_cap"] == "0"
            state.runner.worker(spec, output, "record")
        return SimpleNamespace(returncode=0)

    state.hook = hook
    state.runner.coordinate(state.args)
    result = loads_exact(
        (state.args.output / "blocks" / (state.main["block_id"] + ".json")).read_bytes()
    )
    assert [row["status"] for row in result["rows"]] == ["runtime_error", "truncated"]
    assert paid_client_calls == ["A0"]
    assert any(row[0] == "E2" for row in state.recorded)
    assert result["block_passed"] is False


def test_resume_replays_durable_prefix_without_repeating_interrupted_provider(mock_coordinator):
    state = mock_coordinator
    control = {"checkpoints": [{"label": "tool.before"}], "terminal": None}

    def hook(spec, output, strict):
        if not strict and spec["row"]["experiment"] == "E1" and spec["row"]["config"] == "A0":
            write_json(output.parent / "episode/control.json", control)
            raise KeyboardInterrupt

    state.hook = hook
    with pytest.raises(KeyboardInterrupt):
        state.runner.coordinate(state.args)
    state.runner.coordinate(state.args)
    folder = state.args.output / "episodes" / state.main["block_id"] / "A0/0"
    result = loads_exact((folder / "result.json").read_bytes())
    assert result["status"] == "runtime_error"
    assert result["reason"] == "interrupted_in_flight"
    assert result["interrupted_prefix"] is True
    assert loads_exact((folder / "episode/control.json").read_bytes()) == control
    assert state.recorded.count(("E1", "A0", "0")) == 1
    assert ("E1", "A0", "0") in state.replayed
    assert any(row[0] == "E2" for row in state.recorded)
    block = loads_exact(
        (state.args.output / "blocks" / (state.main["block_id"] + ".json")).read_bytes()
    )
    assert block["replay_passed"] is False


def test_coordinator_full_mock_budget_interruption_resume_and_secret_scan(tmp_path, monkeypatch):
    from scripts import run_live_zh as runner

    root = tmp_path / "repository"
    root.mkdir()
    (root / "docs").mkdir()
    (root / "docs/PREREG-DOMESTIC.md").write_text("synthetic frozen prereg", encoding="utf-8")
    write_json(root / "evidence/probe/requests.json", {"providers": {}})
    write_json(root / "local/models-lock.json", [])
    credentials = tmp_path / "credentials.env"
    secret = "FIXL1_SENTINEL_NEVER_PERSIST_9284"
    credential_text = (
        f"LLM_PROVIDER=openai\nLLM_API_BASE=https://api.openai.com/v1\nOPENAI_API_KEY={secret}\n"
    )
    credentials.write_text(credential_text, encoding="utf-8")
    domestic_credentials = tmp_path / "domestic.env"
    domestic_credentials.write_text(
        "LLM_PROVIDER=qwen-plus\nLLM_API_BASE=https://dashscope.aliyuncs.com/compatible-mode/v1\nLLM_API_KEY=FAKE_QWEN\n",
        encoding="utf-8",
    )
    cases = [{"case_id": f"sample-{index}", "harm_class": "synthetic"} for index in range(2)]
    real_plan = runner.build_plan(cases, domestic_api="qwen-plus")
    blocks = [block for block in real_plan if block["priority"] in {2, 3}]
    original_blocks = [block for block in blocks if block["provider"] != "qwen-plus"]
    monkeypatch.setattr(runner, "ROOT", root)
    monkeypatch.setattr(runner, "LOCAL_ROOT", root / "local")
    monkeypatch.setattr(runner, "load_cases", lambda path: cases)
    monkeypatch.setattr(
        runner, "build_plan", lambda *a, **kw: blocks if kw.get("domestic_api") else original_blocks
    )
    monkeypatch.setattr(
        runner, "BUDGET_CAPS", {"USD": Fraction(10), "CNY": Fraction(50), "LOCAL": Fraction(0)}
    )
    monkeypatch.setattr(runner, "run_formal_gate", lambda **kw: {"ready": True})
    monkeypatch.setattr(runner, "require_global_probe_gate", lambda probe: None)
    monkeypatch.setattr(
        runner,
        "load_run_probe",
        lambda *a: {
            "providers": {
                "qwen-plus": {"api_base": "https://dashscope.aliyuncs.com/compatible-mode/v1"}
            }
        },
    )
    monkeypatch.setattr(runner, "workspace_clean", lambda *a: None)
    monkeypatch.setattr(runner, "verify_local_lock", lambda *a: {"verified": True})
    monkeypatch.setattr(runner, "local_status", lambda: {})
    monkeypatch.setattr(runner, "local_running_models", lambda: {"models": []})
    gpu_checks = []
    monkeypatch.setattr(
        runner, "ensure_local_runtime", lambda *a, **kw: gpu_checks.append(a) or {"verified": True}
    )
    monkeypatch.setattr(
        runner,
        "_profile",
        lambda row, *a: {
            "model": row["model"],
            "provider": row["provider"],
            "api_base": "https://api.openai.com/v1"
            if row["provider"] == "openai"
            else "https://dashscope.aliyuncs.com/compatible-mode/v1",
        },
    )
    monkeypatch.setattr(
        runner.subprocess,
        "check_output",
        lambda command, **kw: "domestic-freeze-2" if "describe" in command else "",
    )

    calls = []
    interrupted = False

    def fake_client(**kwargs):
        if kwargs["mode"] == "record":
            assert kwargs["api_key"] in {secret, "ollama", "FAKE_QWEN"}
        assert kwargs["env_file"] is None
        return SimpleNamespace(
            model=kwargs["model"],
            mode=kwargs["mode"],
            spent_usd=Fraction(1, 100),
            calls=[{"total_tokens": 100}],
        )

    monkeypatch.setattr(runner, "LLMClient", fake_client)
    monkeypatch.setattr(
        runner,
        "run_case",
        lambda *a, **kw: {
            "status": "valid_completed",
            "completed": True,
            "prohibited_effect": False,
        },
    )

    def fake_process(command, **kwargs):
        nonlocal interrupted
        if "--worker" not in command:
            return SimpleNamespace(returncode=0)
        spec = loads_exact(Path(command[command.index("--worker") + 1]).read_bytes())
        row = spec["row"]
        strict = "--strict" in command
        if not strict:
            identity = (row["block_id"], row["config"])
            if (
                row["priority"] == 2
                and len([call for call in calls if call[0] == row["block_id"]]) == 1
                and not interrupted
            ):
                interrupted = True
                raise KeyboardInterrupt
            assert identity not in calls
            calls.append(identity)
        output = Path(command[command.index("--output") + 1])
        selected_env = (
            Path(command[command.index("--env-file") + 1])
            if "--env-file" in command
            else tmp_path / "absent"
        )
        runner.worker(
            spec,
            output,
            "strict" if strict else "record",
            env_file=selected_env,
            provider_env=[command[command.index("--provider-env") + 1]]
            if "--provider-env" in command
            else [],
        )
        if row["priority"] == 2 and row["case_id"] == "sample-0":
            observed = loads_exact(output.read_bytes())
            if row["config"] == "A0":
                observed.update(status="truncated", completed=False, termination="tool_calls")
            elif row["config"] == "A5":
                observed.update(status="runtime_error", error_class="apparatus_defect")
            write_json(output, observed)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(runner.subprocess, "run", fake_process)
    args = Namespace(
        cases=root / "synthetic-cases",
        output=tmp_path / "output",
        domestic_api=None,
        episode_cap="2",
        execute=True,
        env_file=credentials,
    )
    with pytest.raises(KeyboardInterrupt):
        runner.coordinate(args)
    runner.coordinate(args)
    assert all(
        loads_exact(path.read_bytes())["admitted"]
        for path in (args.output / "admissions").glob("*.json")
    )
    pilot = loads_exact((args.output / "pilot-estimate.json").read_bytes())
    assert pilot["passed"] and pilot["in_denominator"] is False
    assert pilot["mean_episode_cost_usd"] == "1/100" and pilot["mean_episode_tokens"] == "100"
    assert len(gpu_checks) == len([block for block in blocks if block["provider"] == "local"])
    before = list(calls)
    journals = {path.name: path.read_bytes() for path in args.output.glob("budget-*.jsonl")}
    runner.coordinate(args)
    assert calls == before
    assert journals == {path.name: path.read_bytes() for path in args.output.glob("budget-*.jsonl")}
    original_plan = (args.output / "plan.json").read_bytes()
    original_results = {
        path.name: path.read_bytes() for path in (args.output / "blocks").glob("*.json")
    }
    args.domestic_api = "qwen-plus"
    args.provider_env = {"qwen-plus": domestic_credentials}
    runner.coordinate(args)
    assert calls[: len(before)] == before
    expected_added = [block for block in blocks if block["provider"] == "qwen-plus"]
    assert len(calls) - len(before) == sum(len(block["configs"]) for block in expected_added)
    assert all(
        (args.output / "blocks" / name).read_bytes() == raw
        for name, raw in original_results.items()
    )
    assert [path.read_bytes() for path in (args.output / "plan-revisions").glob("*.json")] == [
        original_plan
    ]
    args.episode_cap = "1/5"
    with pytest.raises(ValueError, match="frozen run identity"):
        runner.coordinate(args)
    args.episode_cap = "2"
    args.domestic_api = None
    with pytest.raises(ValueError, match="frozen run identity"):
        runner.coordinate(args)
    assert len(calls) - len(before) == sum(len(block["configs"]) for block in expected_added)
    outcomes = [loads_exact(path.read_bytes()) for path in (args.output / "blocks").glob("*.json")]
    main = [row for outcome in outcomes for row in outcome["rows"] if row["priority"] == 2]
    assert all(row["status"] == "valid_completed" for row in main if row["config"] in {"A4", "A1"})
    assert any(row["status"] == "truncated" for row in main)
    assert any(row["status"] == "runtime_error" for row in main)
    assert any(outcome.get("block_passed") is False for outcome in outcomes)
    assert sum(row["status"] == "unknown" for row in main) == 1
    for path in args.output.rglob("*"):
        if path.is_file():
            raw = path.read_bytes()
            assert secret.encode() not in raw
            assert str(credentials).encode() not in raw
    assert credentials.read_text(encoding="utf-8") == credential_text


def test_pilot_reservation_scales_with_observed_cost_not_episode_safety_valve(tmp_path):
    from corridor.bench.live_zh import BudgetJournal, SpendingJournal
    from scripts.run_live_zh import reserve_comparison_block

    blocks = [{"block_id": str(index), "configs": ["A0", "A5"]} for index in range(10)]
    journal = BudgetJournal(tmp_path / "budget.jsonl", "USD", Fraction(10))
    spending = SpendingJournal(tmp_path / "spending.jsonl", "USD", Fraction(10))
    result = reserve_comparison_block(blocks, journal, spending, Fraction(1, 100))
    assert result["admitted"] is True
    assert journal.remaining() == Fraction(97, 10)
    before = journal.path.read_bytes()
    assert reserve_comparison_block(blocks, journal, spending, Fraction(1, 100)) == result
    assert journal.path.read_bytes() == before


def test_advisory_coordinator_lock_releases_for_resume(tmp_path):
    from scripts.run_live_zh import coordinator_lock

    path = tmp_path / "coordinator.lock"
    with coordinator_lock(path), pytest.raises(ValueError, match="Another coordinator"):
        with coordinator_lock(path):
            pytest.fail("Duplicate coordinator acquired the lock")
    with coordinator_lock(path):
        pass


def test_worker_preserves_schedule_identity_over_runner_deployment_labels(tmp_path, monkeypatch):
    from scripts import run_live_zh as runner

    client = SimpleNamespace(spent_usd=Fraction(0), calls=[])
    monkeypatch.setattr(runner, "make_worker_client", lambda *a, **kw: client)
    monkeypatch.setattr(runner, "load_cases", lambda path: [{"case_id": "synthetic-1"}])
    monkeypatch.setattr(
        runner,
        "run_case",
        lambda *a, **kw: {
            "status": "valid_completed",
            "completed": True,
            "prohibited_effect": False,
            "corpus": "C2",
            "model": "qwen3:8b-q4_K_M",
            "mode": "strict-replay",
        },
    )
    row = {
        "experiment": "stress",
        "mode": "live-recorded",
        "model": "qwen3-8b",
        "corpus": "C2-stress",
        "case_id": "synthetic-1",
        "config": "A0",
        "seed": 1,
        "variant": "attack",
        "status": "not_run",
        "completed": False,
        "prohibited_effect": None,
    }
    spec = {"row": row, "currency": "LOCAL", "episode_cap": "1/10", "cases": str(tmp_path)}
    output = tmp_path / "result.json"
    runner.worker(spec, output, "strict")
    actual = loads_exact(output.read_bytes())
    assert actual["model"] == "qwen3-8b" and actual["corpus"] == "C2-stress"
    assert actual["status"] == "valid_completed" and actual["completed"] is True
    assert actual["mode"] == "strict-replay"


def test_complete_registered_plan_dry_run_with_mock_provider(tmp_path):
    from collections import Counter

    from corridor.bench.live_zh import build_pilot_plan, build_plan, execute_block

    samples = [{"case_id": f"sample-{i}", "harm_class": "synthetic"} for i in range(2)]
    blocks = build_pilot_plan(samples) + build_plan(samples, domestic_api="qwen-plus")
    recorded, replayed, experiments = {}, set(), set()
    interrupted_block, failed_block = blocks[3], blocks[2]
    interrupted_identity = (interrupted_block["block_id"], interrupted_block["configs"][1], 0)
    worker_result = tmp_path / "completed-worker-result.json"
    progress = tmp_path / "interrupted-progress.jsonl"
    recovered, interruptions, failures = [], 0, 0
    statuses = Counter()

    def provider(row, attempt):
        identity = (row["block_id"], row["config"], attempt)
        if identity in recorded:
            pytest.fail("Duplicate mock provider episode")
        experiments.add(row["experiment"])
        result = {**row, "status": "valid_completed", "completed": True, "prohibited_effect": False}
        if row["block_id"] == failed_block["block_id"]:
            if row["config"] == "A0":
                result.update(
                    status="truncated",
                    completed=False,
                    error_class="local_resource",
                    termination="budget_truncated",
                    truncation_limit="tool_calls",
                    metrics={"tool_calls": 16, "cost_usd": "1/100"},
                )
            elif row["config"] == "A5":
                result.update(
                    status="runtime_error", completed=False, error_class="apparatus_defect"
                )
        recorded[identity] = result
        if identity == interrupted_identity:
            write_json(worker_result, result)
            raise KeyboardInterrupt
        return result

    def replay(rows):
        for row in rows:
            identity = (row["block_id"], row["config"], row["attempt"])
            if identity in replayed:
                pytest.fail("Duplicate mock replay attempt")
            if {key: value for key, value in row.items() if key != "attempt"} != recorded[identity]:
                pytest.fail("Mock strict observation differs from its recorded attempt")
            replayed.add(identity)
        return True

    def recover(row, attempt):
        identity = (row["block_id"], row["config"], attempt)
        assert identity == interrupted_identity
        recovered.append(identity)
        return loads_exact(worker_result.read_bytes())

    for block in blocks:
        kwargs = {"progress": progress, "recover": recover} if block == interrupted_block else {}
        try:
            result = execute_block(block, provider, replay, **kwargs)
        except KeyboardInterrupt:
            interruptions += 1
            assert block == interrupted_block
            result = execute_block(block, provider, replay, **kwargs)
        assert result["replay_passed"]
        assert result["block_passed"] == (block != failed_block)
        failures += not result["block_passed"]
        statuses.update(row["status"] for row in result["rows"])
    assert experiments == {"pilot", "E1", "E2", "E3", "stress", "E4"}
    planned = sum(len(block["configs"]) for block in blocks)
    assert len(recorded) == len(replayed) == planned
    assert statuses == {"valid_completed": planned - 2, "truncated": 1, "runtime_error": 1}
    assert interruptions == failures == len(recovered) == 1
    assert len(blocks) > 10_000
    write_json(
        Path(".tmp/fix-l2/mock-dryrun.json"),
        {
            "schema": "fix-l2-complete-plan-mock-v1",
            "mode": "mock-provider",
            "real_model_calls": 0,
            "blocks_completed": len(blocks),
            "blocks_passed": len(blocks) - failures,
            "blocks_failed": failures,
            "planned_episodes": planned,
            "provider_attempts": len(recorded),
            "strict_replayed_attempts": len(replayed),
            "episode_statuses": dict(statuses),
            "interruptions": interruptions,
            "recovered_worker_results": len(recovered),
            "duplicate_provider_attempts": 0,
            "experiments": sorted(experiments),
        },
    )


@pytest.mark.parametrize("experiment", ["E2", "E3", "E4"])
def test_other_worker_branches_use_mock_provider_and_propagate_usage(
    tmp_path, monkeypatch, experiment
):
    from scripts import run_live_zh as runner

    client = SimpleNamespace(spent_usd=Fraction(1, 100), calls=[{"total_tokens": 42}])
    monkeypatch.setattr(runner, "make_worker_client", lambda *a, **kw: client)
    result = {"status": "valid_completed", "completed": True, "prohibited_effect": False}
    monkeypatch.setattr(
        "corridor.bench.interaction.run_interaction_episode", lambda *a, **kw: result
    )
    monkeypatch.setattr(
        "corridor.bench.consistency.run_consistency_episode", lambda *a, **kw: result
    )
    monkeypatch.setitem(
        sys.modules, "agentdojo_runner", SimpleNamespace(run_episode=lambda *a, **kw: result)
    )
    spec = {
        "row": {"experiment": experiment, "case_id": "synthetic-1", "config": "A0", "seed": 1},
        "currency": "USD",
        "episode_cap": "1/10",
    }
    output = tmp_path / "result.json"
    runner.worker(spec, output, "record", env_file=tmp_path / "absent.env")
    observed = loads_exact(output.read_bytes())
    assert observed["status"] == "valid_completed"
    assert observed["usage_measurements"] == {
        "cost": "1/100",
        "currency": "USD",
        "total_tokens": 42,
        "model_requests": 1,
        "output_tokens": 0,
        "finish_reason_length": 0,
    }
