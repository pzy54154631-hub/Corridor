"""Sequential whole-block coordinator. Default creates a plan; --execute is explicit."""

import argparse
import hashlib
import os
import re
import subprocess
import sys
from contextlib import contextmanager, nullcontext, redirect_stderr, redirect_stdout
from fractions import Fraction
from pathlib import Path
from urllib.request import Request, urlopen

from corridor.agent.llm import LLMClient, is_output_truncated
from corridor.bench.bank_cases import write_json
from corridor.bench.blind_zh import load_cases, run_case
from corridor.bench.episode_control import finish_interrupted_episode, safe_traceback
from corridor.bench.formal_gate import load_run_probe, run_formal_gate, verify_e4, workspace_clean
from corridor.bench.live_zh import (
    BUDGET_CAPS,
    BudgetJournal,
    SpendingJournal,
    build_pilot_plan,
    build_plan,
    build_rehearsal_plan,
    comparison_groups,
    ensure_local_runtime,
    estimate_group_cost,
    execute_block,
    planned_rows,
    replay_projection,
    report_row_sets,
    schedule_blocks,
    select_row_set,
    verify_local_lock,
    verify_local_runtime,
)
from corridor.bench.recording import network_disabled, observation_hash
from corridor.bench.request_probe import load_provider_credentials
from corridor.crypto.jcs import H, jcs_bytes
from corridor.schemas.base import loads_exact

ROOT = Path(__file__).resolve().parents[1]
LOCAL_ROOT = Path("local-models")
WORKER_TIMEOUT_SECONDS = 630


def provider_env_paths(entries):
    """Provider bindings are runtime arguments and never enter persisted specs."""
    if isinstance(entries, dict):
        entries = [f"{provider}={path}" for provider, path in entries.items()]
    paths = {}
    for entry in entries or []:
        provider, separator, path = entry.partition("=")
        if (
            not separator
            or not path
            or provider in paths
            or provider not in {"deepseek", "zhipu", "qwen-plus"}
        ):
            raise ValueError("Invalid or duplicate provider credential binding")
        paths[provider] = Path(path)
    return paths


def credential_path(provider, env_file, provider_env):
    return Path(env_file) if provider == "openai" else provider_env.get(provider)


def domestic_worker_options(provider):
    """Preserve freeze-6 inherited environments outside domestic API workers."""
    if provider not in {"deepseek", "zhipu"}:
        return {}
    from corridor.bench.request_probe import domestic_worker_environment

    return {"env": domestic_worker_environment()}


def make_worker_client(spec, mode, *, env_file=ROOT / ".env", provider_env=None):
    row = spec["row"]
    profile = spec["client_profile"]
    api_key = None
    if mode == "record":
        if row["provider"] == "local":
            api_key = "ollama"
        else:
            if profile.get("provider") != row["provider"]:
                raise ValueError("Worker provider differs from bound probe")
            credentials = load_provider_credentials(
                credential_path(row["provider"], env_file, provider_env_paths(provider_env)),
                provider=row["provider"],
                api_base=profile["api_base"],
                model=profile["model"] if row["provider"] != "openai" else None,
            )
            if credentials is None:
                raise ValueError("missing_credentials")
            api_key = credentials.api_key
    options = {}
    if row.get("experiment") == "E4":
        from corridor.bench.agentdojo_contract import synthetic_context_guard

        options = {
            "tool_namespace": "agentdojo-banking-v1.2.2",
            "context_guard": synthetic_context_guard,
        }
    return LLMClient(
        mode=mode,
        replay_dir=Path(spec["replay_dir"]),
        config=row["config"],
        user_policy="goal_bound",
        env_file=None,
        max_calls=64,
        max_usd="1000000" if row["provider"] == "local" else spec["episode_cap"],
        api_key=api_key,
        **profile,
        **options,
    )


def worker(spec, output, mode, *, env_file=ROOT / ".env", provider_env=None):
    """Persist any apparatus exception before the subprocess exits."""
    clients = []
    try:
        _worker(spec, output, mode, env_file=env_file, provider_env=provider_env, clients=clients)
    except Exception as error:
        write_json(
            output,
            {
                **spec["row"],
                "status": "runtime_error",
                "completed": False,
                "prohibited_effect": None,
                "error_class": "apparatus_defect",
                "reason": type(error).__name__,
                "traceback": safe_traceback(error),
                **(
                    {
                        "usage_measurements": {
                            "cost": str(clients[0].spent_usd),
                            "currency": spec["currency"],
                        }
                    }
                    if clients
                    else {}
                ),
            },
        )


def _worker(spec, output, mode, *, env_file=ROOT / ".env", provider_env=None, clients=None):
    row = spec["row"]
    if spec["currency"] != "LOCAL" and Fraction(spec["episode_cap"]) == 0:
        # The coordinator reserved zero remaining shared allowance. There is no
        # model observation to replay and no client, fixture, or tool is initialized.
        result = {
            **row,
            "mode": "strict-replay" if mode == "strict" else "live-recorded",
            "status": "truncated",
            "completed": False,
            "prohibited_effect": None,
            "truncated": True,
            "error_class": "local_resource",
            "termination": "budget_truncated",
            "truncation_limit": "shared_budget",
            "cost_currency": spec["currency"],
            "episode_safety_ceiling": "0",
            "early_stop": {
                "stage": "before_worker_client",
                "reason": "shared_budget_exhausted",
                "model_requests": 0,
                "tool_calls": 0,
            },
            "usage_measurements": {"cost": "0", "currency": spec["currency"], "total_tokens": 0},
            "metrics": {"user_turns": 0, "model_calls": 0, "tool_calls": 0, "cost_usd": "0"},
        }
        write_json(output, result)
        return
    client = make_worker_client(spec, mode, env_file=env_file, provider_env=provider_env)
    if clients is not None:
        clients.append(client)
    original = (
        loads_exact(Path(spec["result_path"]).read_bytes())
        if mode == "strict" and spec.get("result_path") and Path(spec["result_path"]).is_file()
        else None
    )
    if row["experiment"] in {"E1", "stress", "pilot", "rehearsal"}:
        cases = {case["case_id"]: case for case in load_cases(Path(spec["cases"]))}
        result = run_case(
            cases[row["case_id"]],
            config=row["config"],
            client=client,
            seed=row["seed"],
            variant=row["variant"],
            evidence_dir=output.parent / "episode",
            replay_observation=original,
        )
    elif row["experiment"] == "E2":
        from corridor.bench.interaction import run_interaction_episode

        result = run_interaction_episode(
            row["case_id"],
            client=client,
            config=row["config"],
            seed=row["seed"],
            output=output.parent / "episode",
            replay_observation=original,
        )
    elif row["experiment"] == "E3":
        from corridor.bench.consistency import run_consistency_episode

        result = run_consistency_episode(
            row["case_id"],
            client=client,
            config=row["config"],
            seed=row["seed"],
            output=output.parent / "episode",
            replay_observation=original,
        )
    elif row["experiment"] == "E4":
        from agentdojo_runner import run_episode

        result = run_episode(
            row, client, replay_observation=original, evidence_dir=output.parent / "episode"
        )
    else:
        raise ValueError("Unknown experiment")
    result = {
        **row,
        **result,
        **{
            key: value
            for key, value in row.items()
            if key not in {"status", "completed", "prohibited_effect", "mode"}
            and not (row["experiment"] == "E2" and key in {"corpus", "variant"})
        },
        "cost_currency": spec["currency"],
        "episode_safety_ceiling": spec["episode_cap"],
        "usage_measurements": {
            "cost": str(client.spent_usd),
            "currency": spec["currency"],
            "model_requests": len(client.calls),
            "output_tokens": sum(
                call.get("output_tokens", call.get("recorded_usage", {}).get("output_tokens", 0))
                for call in client.calls
            ),
            "finish_reason_length": sum(is_output_truncated(call) for call in client.calls),
            "total_tokens": sum(
                call.get("total_tokens", call.get("recorded_usage", {}).get("total_tokens", 0))
                for call in client.calls
            ),
        },
    }
    write_json(output, result)


def local_running_models():
    """Query running models only; this endpoint cannot load or start a model."""
    try:
        with urlopen("http://127.0.0.1:11434/api/ps", timeout=10) as response:
            return loads_exact(response.read())
    except Exception:
        raise ValueError("Local block stopped: /api/ps unavailable") from None


def load_local_model(model, timeout):
    """An empty generation request loads weights without generating experiment tokens."""
    request = Request(
        "http://127.0.0.1:11434/api/generate",
        data=jcs_bytes({"model": model, "prompt": "", "stream": False, "keep_alive": "10m"}),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=timeout) as response:
        response.read()


@contextmanager
def coordinator_lock(path, *, readonly=False):
    """OS ownership expires on a crash, so durable progress can be resumed safely."""
    if readonly and not path.exists():
        yield
        return
    with path.open("rb" if readonly else "a+b") as stream:
        if not readonly and stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ValueError("Another coordinator owns this output directory") from None
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def require_finished_source(source):
    """Inspect a completed source without creating or changing its lock file."""
    with coordinator_lock(source / ".coordinator.lock", readonly=True):
        plan = loads_exact((source / "plan.json").read_bytes())
        missing = [
            block["block_id"]
            for block in plan.get("blocks", [])
            if not (source / "blocks" / (block["block_id"] + ".json")).is_file()
        ]
        if missing:
            raise ValueError(
                "Continuation source incomplete; missing blocks: " + ", ".join(missing)
            )


@contextmanager
def idle_local_source(source):
    """An existing source lock is required for an actual, read-only GPU exclusion."""
    path = source / ".coordinator.lock"
    if not path.is_file():
        raise ValueError("Local rerun requires an existing source coordinator lock")
    with coordinator_lock(path, readonly=True):
        yield


def source_result_hashes(source):
    files = sorted((source / "blocks").glob("*.json")) + sorted(
        (source / "episodes").glob("*/*/*/result.json")
    )
    return {
        path.relative_to(source).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in files
    }


def missing_source_attempts(source, hashes):
    available = {
        name.removeprefix("episodes/").removesuffix("/result.json")
        for name in hashes
        if name.startswith("episodes/")
    }
    missing = []
    for name in hashes:
        if not name.startswith("blocks/"):
            continue
        report = loads_exact((source / name).read_bytes())
        for row in report.get("attempts", []):
            identity = f"{Path(name).stem}/{row['config']}/{row['attempt']}"
            if identity not in available:
                missing.append(identity)
        for row in report.get("rows", []):
            if row.get("status") in {"not_run", "invalid_case"}:
                continue
            prefix = f"{Path(name).stem}/{row['config']}/"
            if not any(identity.startswith(prefix) for identity in available):
                missing.append(prefix + "missing-observation")
    return sorted(set(missing))


def replay_code_hashes():
    files = sorted((ROOT / "src/corridor").rglob("*.py")) + [
        ROOT / "scripts/run_live_zh.py",
        ROOT / "scripts/agentdojo_run.py",
        ROOT / "scripts/agentdojo_runner.py",
    ]
    return {
        path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in files
        if path.is_file()
    }


def strict_source_worker(spec, destination):
    """Run the existing worker offline; E4 retains its pinned apparatus interpreter."""
    if spec["row"]["experiment"] == "E4":
        spec_path = destination.parent / "spec.json"
        write_json(spec_path, spec)
        process = subprocess.run(
            [
                str(ROOT / ".tmp/agentdojo-venv/Scripts/python.exe"),
                str(ROOT / "scripts/agentdojo_run.py"),
                "--worker",
                str(spec_path),
                "--strict",
                "--output",
                str(destination),
            ],
            cwd=ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=WORKER_TIMEOUT_SECONDS,
            env={
                **os.environ,
                "PYTHONPATH": os.pathsep.join(
                    str(path) for path in (ROOT / "src", ROOT / "scripts", ROOT)
                ),
            },
        )
        return process.returncode == 0
    with network_disabled(), open(os.devnull, "w", encoding="utf-8") as sink:
        with redirect_stdout(sink), redirect_stderr(sink):
            worker(spec, destination, "strict", env_file=None)
    return True


def prove_source_replay(source, output):
    """Produce a complete, content-bound offline replay proof, including all providers."""
    source, output = source.resolve(), output.resolve()
    if output.is_relative_to(source):
        raise ValueError("Replay proof output must be outside the read-only source")
    unit_root = output.parent / (output.stem + "-units")
    if unit_root.exists():
        raise ValueError("Replay proof requires a fresh unit directory; choose a new output path")
    before = source_result_hashes(source)
    results = [name for name in before if name.startswith("episodes/")]
    proof = {
        "schema": "zh-source-replay-proof-v1",
        "network_disabled": True,
        "source_plan_sha256": hashlib.sha256((source / "plan.json").read_bytes()).hexdigest(),
        "source_result_sha256": before,
        "replay_code_sha256": replay_code_hashes(),
        "units": len(results),
        "replayed": 0,
        "consistent": 0,
        "inconsistent": 0,
        "inconsistent_ids": [],
        "unit_results": [],
        "missing_source_attempts": missing_source_attempts(source, before),
    }
    for relative in results:
        original_path = source / relative
        identity = original_path.parent.relative_to(source / "episodes").as_posix()
        destination = unit_root / identity / "result.json"
        matched = False
        try:
            spec = loads_exact((original_path.parent / "spec.json").read_bytes())
            # Only recorded repository paths are rebound; episode artifacts always
            # resolve inside the read-only snapshot rather than the live source.
            old_recording = Path(spec.get("replay_dir", ""))
            old_root = next((p for p in old_recording.parents if p.name == "evidence"), None)
            if old_root is not None and spec.get("cases"):
                try:
                    spec["cases"] = str(ROOT / Path(spec["cases"]).relative_to(old_root.parent))
                except ValueError:
                    pass
            spec["replay_dir"] = str(original_path.parent / "recording")
            spec["result_path"] = str(original_path)
            success = strict_source_worker(spec, destination)
            original = loads_exact(original_path.read_bytes())
            matched = bool(
                success
                and destination.is_file()
                and not original.get("interrupted_prefix")
                and observation_hash(replay_projection(original))
                == observation_hash(replay_projection(loads_exact(destination.read_bytes())))
            )
        except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
            matched = False
        proof["replayed"] += 1
        proof["consistent" if matched else "inconsistent"] += 1
        proof["unit_results"].append({"id": identity, "consistent": matched})
        if not matched:
            proof["inconsistent_ids"].append(identity)
        write_json(output, proof)
    proof["source_unchanged"] = before == source_result_hashes(source)
    proof["passed"] = (
        proof["source_unchanged"]
        and proof["inconsistent"] == 0
        and not proof["missing_source_attempts"]
    )
    write_json(output, proof)
    return proof


def require_source_replay_proof(source, proof_path):
    if not proof_path.is_file():
        raise ValueError("Continuation requires source-replay-proof.json; run --prove-source first")
    proof = loads_exact(proof_path.read_bytes())
    hashes = source_result_hashes(source)
    identities = [
        Path(name).parent.relative_to("episodes").as_posix()
        for name in hashes
        if name.startswith("episodes/")
    ]
    if (
        proof.get("schema") != "zh-source-replay-proof-v1"
        or proof.get("source_result_sha256") != hashes
        or proof.get("source_plan_sha256")
        != hashlib.sha256((source / "plan.json").read_bytes()).hexdigest()
        or proof.get("replay_code_sha256") != replay_code_hashes()
        or proof.get("network_disabled") is not True
        or proof.get("passed") is not True
        or proof.get("source_unchanged") is not True
        or proof.get("units") != len(identities)
        or proof.get("replayed") != len(identities)
        or proof.get("consistent") != len(identities)
        or proof.get("inconsistent") != 0
        or proof.get("missing_source_attempts") != []
        or proof.get("unit_results")
        != [{"id": identity, "consistent": True} for identity in identities]
    ):
        raise ValueError("Continuation source replay proof hash, coverage, or consistency mismatch")
    if missing_source_attempts(source, hashes):
        raise ValueError("Continuation replay proof lacks source attempt coverage")
    return proof


def pilot_estimate(rows, *, replay_passed):
    cost = sum(
        (Fraction(row.get("usage_measurements", {}).get("cost", "0")) for row in rows), Fraction(0)
    )
    return {
        "schema": "zh-r5-pilot-v1",
        "in_denominator": False,
        "planned": 4,
        "completed": len(rows),
        "passed": replay_passed
        and len(rows) == 4
        and cost > 0
        and all(
            row["status"] == "valid_completed"
            and "cost" in row.get("usage_measurements", {})
            and "total_tokens" in row.get("usage_measurements", {})
            for row in rows
        ),
        "replay_passed": replay_passed,
        "total_cost_usd": str(cost),
        "mean_episode_cost_usd": str(cost / len(rows)) if rows else "0",
        "mean_episode_tokens": str(
            Fraction(
                sum(row.get("usage_measurements", {}).get("total_tokens", 0) for row in rows),
                len(rows),
            )
        )
        if rows
        else "0",
    }


def reserve_comparison_block(group, journal, spending, mean_cost, safety_factor=Fraction(3, 2)):
    """Admit all configurations, variants and seeds using measured remaining spend."""
    identity = H([block["block_id"] for block in group])[:24]
    episodes = sum(len(block["configs"]) for block in group)
    cost = estimate_group_cost(mean_cost, episodes, safety_factor=safety_factor)
    admitted = True
    try:
        journal.reserve(identity, cost, available=spending.remaining())
    except ValueError as error:
        if str(error) != "Whole-block budget exhausted":
            raise
        admitted = False
    return {
        "comparison_block_id": identity,
        "admitted": admitted,
        "episode_count": episodes,
        "reserved": str(cost),
        "mean_episode_cost": str(mean_cost),
        "safety_factor": str(safety_factor),
        "retry_reservation": "0",
    }


def _status_error(stderr=None):
    """Bound diagnostics and redact credentials without reading any dotenv file."""
    diagnostic = stderr or ""
    if isinstance(diagnostic, bytes):
        diagnostic = diagnostic.decode("utf-8-sig", errors="replace")
    for name, value in os.environ.items():
        if value and re.search(r"key|token|secret|password|credential|authorization", name, re.I):
            diagnostic = diagnostic.replace(value, "[redacted]")
    lines = []
    redact_remainder = False
    for line in diagnostic.splitlines()[:5]:
        if re.search(
            r"api[_ -]?key|authorization|bearer|password|secret|credential|"
            r"\b[\w-]*(?:key|token)\b[\"']?\s*[:=]|\bsk-[\w-]+",
            line,
            re.I,
        ):
            # A credential value may continue onto subsequent PowerShell error lines.
            redact_remainder = True
        if redact_remainder:
            line = "[redacted credential-bearing stderr]"
        lines.append(line[:300])
    return ValueError(
        "status.ps1 无法执行或返回不完整; stderr (first 5 lines, redacted):\n"
        + ("\n".join(lines) or "[empty]")
    )


def _status_complete(status):
    """Missing fields are probe failures; explicit false values remain gate failures."""
    if not isinstance(status, dict):
        return False
    version = status.get("version")
    available = status.get("available_models")
    if (
        not all(
            isinstance(status.get(field), bool)
            for field in ("managed_process_running", "loopback_only_verified")
        )
        or "api_error" not in status
        or (status["api_error"] is not None and not isinstance(status["api_error"], str))
        or not isinstance(version, dict)
        or not isinstance(version.get("version"), str)
        or not version["version"].strip()
        or not isinstance(available, dict)
        or not isinstance(available.get("models"), list)
    ):
        return False
    return all(
        isinstance(model, dict)
        and all(
            isinstance(model.get(key), str) and model[key].strip() for key in ("name", "digest")
        )
        for model in available["models"]
    )


def local_status():
    command = (
        "$ErrorActionPreference = 'Stop'; "
        "$s = & 'local-models\\status.ps1' | ConvertFrom-Json; "
        "[ordered]@{managed_process_running=$s.managed_process_running; "
        "loopback_only_verified=$s.loopback_only_verified; api_error=$s.api_error; "
        "version=@{version=[string]$s.version.version}; "
        "available_models=@{models=@($s.available_models.models | ForEach-Object { "
        "@{name=[string]$_.name;digest=[string]$_.digest} })}} | ConvertTo-Json -Depth 6"
    )
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise _status_error(getattr(error, "stderr", None)) from None
    if completed.returncode:
        raise _status_error(completed.stderr) from None
    # The read-only status projection contains only booleans and fixed string identities.
    try:
        status = loads_exact(completed.stdout.decode("utf-8-sig"))
    except (UnicodeError, ValueError):
        raise _status_error(completed.stderr) from None
    if not _status_complete(status):
        raise _status_error(completed.stderr) from None
    return status


def record_worker_interruption(folder, row, reason, error=None):
    """Retain any durable prefix without inventing a final business observation."""
    result_path = folder / "result.json"
    if result_path.is_file():
        retained = folder / "interrupted-worker-result.json"
        if not retained.exists():
            retained.write_bytes(result_path.read_bytes())
    result = {
        **row,
        "status": "runtime_error",
        "completed": False,
        "prohibited_effect": None,
        "error_class": "apparatus_defect",
        "reason": reason,
        "traceback": safe_traceback(error)
        if error is not None
        else "Original child traceback unavailable after process interruption.",
        "original_worker_traceback_available": False,
        "interrupted_prefix": True,
    }
    control = folder / "episode/control.json"
    if control.is_file():
        try:
            result.update(finish_interrupted_episode(control, reason, error=error))
        except (OSError, ValueError, KeyError, TypeError):
            result["checkpoint_error"] = "unreadable_checkpoint"
    write_json(result_path, result)
    return result


def e4_formal_gate_ready(calibration, bridge, root=ROOT):
    """Mock bridge success cannot upgrade a partial native mapping to formal E4."""
    if not calibration.get("calibration_passed") or not calibration.get("formal_e4_gate"):
        return False
    hashes = bridge.get("code_hashes", {})
    return (
        bridge.get("passed") is True
        and bool(hashes)
        and all(
            hashlib.sha256((root / relative).read_bytes()).hexdigest() == expected
            for relative, expected in hashes.items()
        )
    )


def require_global_probe_gate(probe):
    required = {
        "openai": {"gpt-6.1-sol", "gpt-5.4-mini"},
        "local": {"qwen3.8:27b-q4_K_M", "qwen3:8b-q4_K_M", "qwen3.5:4b-q4_K_M"},
    }
    for provider, names in required.items():
        group = probe.get("providers", {}).get(provider, {})
        passed = {
            row.get("model")
            for row in group.get("models", [])
            if row.get("status") == "valid_completed" and row.get("strict_replay") is True
        }
        if group.get("gate_passed") is not True or not names <= passed:
            raise ValueError(
                f"Mandatory request probe or strict replay gate unavailable: {provider}"
            )


def _profile(row, lock, probe):
    local = row["provider"] == "local"
    provider = row["provider"]
    domestic = provider in {"deepseek", "zhipu", "qwen-plus"}
    names = {"qwen3.8-27b": "strong", "qwen3-8b": "medium", "qwen3.5-4b": "weak"}
    locked = next((model for model in lock if model["tier"] == names.get(row["model"])), None)
    actual_model = locked["model"] if locked else row["model"]
    group = probe["providers"].get(provider, {})
    if group.get("provider", provider) != provider:
        raise ValueError("Probe provider declaration differs from its group")
    if not group.get("gate_passed"):
        raise ValueError(f"Request parameter gate unavailable for {provider}")
    models = group.get("models", [])
    observed = next(
        (item for item in models if item["model"] in {actual_model, row["model"]}), None
    )
    if observed is None:
        raise ValueError("Deployment lacks an actual outbound request probe")
    if provider in {"deepseek", "zhipu"} and (
        not observed.get("formal_shape")
        or observed.get("strict_replay") is not True
        or observed.get("deployment_profile", {}).get("thinking") != "disabled"
        or observed.get("deployment_profile", {}).get("tool_choice_mode")
        not in {"forced_function", "auto"}
        or any(
            observed.get("formal_gate_checks", {}).get(key) is not True
            for key in (
                "finish_reason",
                "output_tokens_at_most_300",
                "reasoning_tokens_zero",
                "strict_replay",
            )
        )
    ):
        raise ValueError("Domestic deployment lacks a passing non-thinking formal-shape probe")
    if domestic and (not group.get("api_base") or group.get("pricing_currency") != "CNY"):
        raise ValueError("Domestic API requires a probed base and frozen CNY token prices")
    return {
        "model": actual_model,
        "provider": "ollama" if local else provider,
        "api": "chat" if local or domestic else "responses",
        "api_base": "http://127.0.0.1:11434/v1"
        if local
        else group.get("api_base", "https://api.openai.com/v1"),
        "reasoning_effort": observed["deployment_profile"].get("reasoning_effort"),
        "seed": row["seed"] if observed["parameters"]["seed"]["status"] == "supported" else None,
        "max_output_tokens": 512,
        "request_timeout_seconds": 120,
        "deployment_profile": {
            **observed["deployment_profile"],
            "max_output_tokens": 512,
            "tool_budget": 16,
            "repeat_seed": row["seed"],
            "probe_hash": H(observed),
            "lock_hash": H(locked) if locked else None,
        },
        **(
            {
                "assumed_price_in": group["price_in_per_million"],
                "assumed_price_out": group["price_out_per_million"],
            }
            if domestic
            else {}
        ),
        **({"assumed_price_in": "1", "assumed_price_out": "1"} if local else {}),
    }


def retain_plan(path, manifest):
    """R5 permits only adding the delayed domestic E1 row to a frozen plan."""
    if path.exists():
        previous_bytes = path.read_bytes()
        previous = loads_exact(previous_bytes)
        if previous == manifest:
            return
        if manifest.get("row_set", "all") != "all":
            raise ValueError("Resume plan differs from the frozen run identity")
        old_fixed = {key: value for key, value in previous.items() if key not in {"blocks", "rows"}}
        new_fixed = {key: value for key, value in manifest.items() if key not in {"blocks", "rows"}}
        old_blocks = {block["block_id"]: block for block in previous["blocks"]}
        retained = [block for block in manifest["blocks"] if block["block_id"] in old_blocks]
        added = [block for block in manifest["blocks"] if block["block_id"] not in old_blocks]
        if (
            old_fixed != new_fixed
            or retained != previous["blocks"]
            or previous["rows"] != [row for block in retained for row in planned_rows(block)]
            or not added
            or any(
                block["provider"] not in {"domestic", "deepseek", "zhipu", "qwen-plus"}
                or block["experiment"] != "E1"
                or block["priority"] != 3
                or (
                    block["provider"] not in {"zhipu", "deepseek"}
                    and block["model"] not in {"deepseek-chat", "qwen-plus"}
                )
                or sorted(block["configs"]) != ["A0", "A5", "B_prompt"]
                for block in added
            )
        ):
            raise ValueError("Resume plan differs from the frozen run identity")
        revision = (
            path.parent / "plan-revisions" / (hashlib.sha256(previous_bytes).hexdigest() + ".json")
        )
        revision.parent.mkdir(parents=True, exist_ok=True)
        if revision.exists():
            if revision.read_bytes() != previous_bytes:
                raise ValueError("Retained plan revision differs from its hash")
        else:
            with revision.open("xb") as stream:
                stream.write(previous_bytes)
                stream.flush()
                os.fsync(stream.fileno())
    write_json(path, manifest)


def coordinate(args):
    safety_factor = Fraction(getattr(args, "safety_factor", "3/2"))
    if safety_factor != Fraction(3, 2) or Fraction(args.episode_cap) <= 0:
        raise ValueError("Positive episode safety ceiling and FIX-L3 safety factor 3/2 required")
    stages = set(filter(None, getattr(args, "stages", "").split(",")))
    if stages - {"pilot", "E1", "E2", "E3", "stress", "E4"}:
        raise ValueError("Unknown experiment stage")
    rehearsal = getattr(args, "rehearsal", False)
    row_set = getattr(args, "row_set", "all")
    continuation = getattr(args, "continuation_of", None)
    rerun_manifest = getattr(args, "rerun_manifest", None)
    provider_env = provider_env_paths(getattr(args, "provider_env", None))
    env_file = Path(getattr(args, "env_file", ROOT / ".env"))
    if (stages and row_set != "all") or (rehearsal and row_set in {"cut-continuation", "rerun"}):
        raise ValueError("Selected row sets cannot change stages or use rehearsal")
    if (row_set == "rerun") != (rerun_manifest is not None):
        raise ValueError("--rerun-manifest requires --row-set rerun exclusively")
    if row_set != "rerun" and getattr(args, "rerun_proof", None) is not None:
        raise ValueError("--rerun-proof requires --row-set rerun")
    source_plan = None
    if row_set == "rerun":
        if args.domestic_api or continuation is not None:
            raise ValueError("Rerun cannot add deployments or continuation sources")
        blocks, selection = select_row_set(
            [], row_set="rerun", rerun_manifest=rerun_manifest, require_final=args.execute
        )
        proof_path = getattr(args, "rerun_proof", None)
        if args.execute and proof_path is None:
            raise ValueError("Rerun execution requires --rerun-proof")
        if proof_path is not None:
            from corridor.bench.rerun_proof import verify_bundle

            proof_identity = verify_bundle(
                proof_path,
                manifest_paths=[rerun_manifest],
                replay_code_sha256=replay_code_hashes(),
            )
            selection["rerun_proof"] = {
                "path": proof_identity["bundle_path"],
                "sha256": proof_identity["bundle_sha256"],
            }
        continuation = Path(selection["source_directory"])
        source_plan = loads_exact(Path(selection["source_plan"]).read_bytes())
        if args.output.resolve().is_relative_to(
            continuation.resolve()
        ) or continuation.resolve().is_relative_to(args.output.resolve()):
            raise ValueError("Rerun requires an independent output directory")
        if Fraction(source_plan["episode_cap"]) != Fraction(args.episode_cap):
            raise ValueError("Rerun episode ceiling differs from source plan")
        if (
            source_plan.get("domestic_episode_cap_cny", "15") != "15"
            or Fraction(source_plan.get("budget_safety_factor", "3/2")) != safety_factor
        ):
            raise ValueError("Rerun source episode/budget limits differ")
        if args.execute and any(block["provider"] == "local" for block in blocks):
            with idle_local_source(continuation):
                pass
    elif row_set == "cut-continuation":
        if args.domestic_api:
            raise ValueError("Continuation cannot add domestic deployments")
        require_finished_source(Path(continuation))
        blocks, selection = select_row_set([], row_set=row_set, continuation_of=continuation)
        source_directory = Path(continuation).resolve()
        destination_directory = args.output.resolve()
        if destination_directory.is_relative_to(
            source_directory
        ) or source_directory.is_relative_to(destination_directory):
            raise ValueError("Continuation requires an independent output directory")
        if not blocks:
            print("No budget-cutoff blocks; no model calls")
            return
        source_plan = loads_exact((Path(continuation) / "plan.json").read_bytes())
        selection.update(
            source_directory=str(source_directory),
            replaced_block_ids=[block["block_id"] for block in blocks],
            merge_rule="whole_block_replacement",
        )
        if Fraction(source_plan["episode_cap"]) != Fraction(args.episode_cap):
            raise ValueError("Continuation episode ceiling differs from source plan")
    elif continuation is not None:
        raise ValueError("--continuation-of requires --row-set cut-continuation")
    if rehearsal:
        args.cases = ROOT / "tests/fixtures/blind-zh-sample"
        rehearsal_root = (ROOT / "evidence/rehearsal").resolve()
        if not args.output.resolve().is_relative_to(rehearsal_root):
            raise ValueError("Rehearsal output must be inside evidence/rehearsal")
    cases = load_cases(args.cases)
    samples = load_cases(ROOT / "tests/fixtures/blind-zh-sample")
    pilots = [] if rehearsal or source_plan is not None else build_pilot_plan(samples)
    if source_plan is None:
        probe_path = ROOT / "evidence/probe/requests.json"
        plan_probe = loads_exact(probe_path.read_bytes()) if probe_path.is_file() else {}
        blocks = (
            build_rehearsal_plan(samples, domestic_api=args.domestic_api, probe=plan_probe)
            if rehearsal
            else pilots + build_plan(cases, domestic_api=args.domestic_api, probe=plan_probe)
        )
        blocks, selection = select_row_set(schedule_blocks(blocks), row_set=row_set)
    elif source_plan.get("case_hash") != H(cases):
        raise ValueError("Continuation case hash differs from source plan")
    groups = comparison_groups(blocks)
    groups_by_block = {block["block_id"]: group for group in groups for block in group}
    budget_caps = {**BUDGET_CAPS, "USD": Fraction(2)} if rehearsal else BUDGET_CAPS
    args.output.mkdir(parents=True, exist_ok=True)
    superseded = None
    if source_plan is not None and (row_set != "rerun" or args.execute):
        if row_set != "rerun":
            require_source_replay_proof(
                Path(continuation), args.output / "source-replay-proof.json"
            )
        superseded = []
        for block in blocks:
            if block["block_id"] in selection.get("first_run_block_ids", []):
                continue
            source_report = loads_exact(
                (Path(continuation) / "blocks" / (block["block_id"] + ".json")).read_bytes()
            )
            superseded.extend(
                {**row, "superseded_by_continuation": True} for row in source_report.get("rows", [])
            )
    manifest = {
        "schema_version": "zh-live-plan-v4",
        **selection,
        "rehearsal": rehearsal,
        "in_denominator": not rehearsal,
        "case_hash": H(cases),
        "prereg_hash": H((ROOT / "docs/PREREG-DOMESTIC.md").read_bytes().hex()),
        "blocks": blocks,
        "rows": [row for block in blocks for row in planned_rows(block)],
        "formal_live_executed": False,
        "episode_cap": str(Fraction(args.episode_cap)),
        "domestic_episode_cap_cny": "15",
        "budget_safety_factor": str(safety_factor),
        "budget_caps": {currency: str(cap) for currency, cap in budget_caps.items()},
    }
    retain_plan(args.output / "plan.json", manifest)
    if superseded is not None:
        archive_path = args.output / "superseded-source-rows.json"
        archive_bytes = jcs_bytes({"rows": superseded})
        if archive_path.exists():
            if archive_path.read_bytes() != archive_bytes:
                raise ValueError("Frozen superseded-source archive differs; refusing overwrite")
        else:
            with archive_path.open("xb") as stream:
                stream.write(archive_bytes)
    if not args.execute:
        print(f"Plan only: {len(blocks)} comparison blocks; no model calls")
        return
    if not rehearsal:
        # R4 uses the actual frozen author corpus; missing author cases must never
        # be fabricated to reach the original target of 24.
        case_dir = args.cases / "cases" if (args.cases / "cases").is_dir() else args.cases
        preflight = run_formal_gate(
            root=ROOT, cases=case_dir, frozen_manifest=case_dir.parent / "FROZEN-SHA256.txt"
        )
        write_json(args.output / "preflight-gate.json", preflight)
        if not preflight["ready"]:
            raise ValueError("Mandatory formal-gate is NOT READY; see preflight-gate.json")
        if not cases:
            raise ValueError("Formal C2 requires a nonempty frozen author corpus")
        git = ["git", "-c", f"safe.directory={ROOT.as_posix()}"]
        tag = subprocess.check_output(
            [*git, "describe", "--tags", "--exact-match"], encoding="utf-8"
        ).strip()
        if not tag.startswith("domestic-freeze-"):
            raise ValueError("Formal run requires an exact domestic-freeze tag")
        workspace_clean(ROOT)
    if subprocess.run(
        [
            sys.executable,
            "-c",
            "from corridor.cli import app; app()",
            "witness",
            "--output",
            str(args.output / "witness"),
        ],
        cwd=ROOT,
    ).returncode:
        raise ValueError("Mandatory witness gate failed")
    probe = load_run_probe(
        ROOT / "evidence/probe/requests.json", ROOT / "evidence/fix-l3/mini-probe/requests.json"
    )
    # Validate every present binding before starting any model worker. Missing
    # bindings remain explicit not_run rows; they never inherit another key.
    credentials_available = {}
    for provider in sorted({block["provider"] for block in blocks} - {"local"}):
        group = probe.get("providers", {}).get(provider, {})
        if group.get("provider", provider) != provider:
            raise ValueError("Probe provider declaration differs from its group")
        base = group.get("api_base", "https://api.openai.com/v1" if provider == "openai" else "")
        credentials_available[provider] = all(
            load_provider_credentials(
                credential_path(provider, env_file, provider_env),
                provider=provider,
                api_base=base,
                model=model if provider != "openai" else None,
            )
            is not None
            for model in sorted({b["model"] for b in blocks if b["provider"] == provider})
        )
    if row_set != "domestic-api":
        try:
            require_global_probe_gate(probe)
        except ValueError as error:
            write_json(args.output / "preflight-gate.json", {"passed": False, "reason": str(error)})
            raise
    lock = []
    if any(block["provider"] == "local" for block in blocks):
        lock = loads_exact((LOCAL_ROOT / "models-lock.json").read_bytes())
        checked = verify_local_lock(lock, local_status())
        write_json(args.output / "local-lock-verification.json", checked)
    journals = {
        currency: BudgetJournal(
            args.output / f"budget-{currency}.jsonl",
            currency,
            budget_caps[currency],
        )
        for currency in ("USD", "CNY", "LOCAL")
    }
    spending = {
        currency: SpendingJournal(args.output / f"spending-{currency}.jsonl", currency, cap)
        for currency, cap in budget_caps.items()
        if currency != "LOCAL"
    }
    source_lock = (
        idle_local_source(Path(continuation))
        if row_set == "rerun" and any(block["provider"] == "local" for block in blocks)
        else nullcontext()
    )
    with source_lock, coordinator_lock(args.output / ".coordinator.lock"):
        if row_set == "rerun":
            # Recheck first-run absence and existing-result hashes after taking the lock.
            from corridor.bench.rerun_zh import load_manifest

            load_manifest(rerun_manifest, require_final=True)
            verify_bundle(
                selection["rerun_proof"]["path"],
                expected_sha256=selection["rerun_proof"]["sha256"],
                manifest_paths=[rerun_manifest],
                replay_code_sha256=replay_code_hashes(),
            )
        paid_pilots = []
        development_pilots = []
        paid_replay_passed = True
        development_replay_passed = True
        estimate = None
        if row_set == "domestic-api" or (
            row_set == "rerun"
            and all(block["provider"] in {"deepseek", "zhipu", "qwen-plus"} for block in blocks)
        ):
            # No OpenAI pilot may be introduced into a domestic-only run. Reserve
            # the full CNY episode ceiling for every comparison before admission.
            estimate = {"passed": True, "basis": "domestic_episode_ceiling_cny"}
        if source_plan is not None and any(block["provider"] == "openai" for block in blocks):
            selected_ids = {block["block_id"] for block in blocks}
            for original_block in source_plan["blocks"]:
                if original_block["block_id"] in selected_ids:
                    continue
                if original_block["experiment"] != "pilot" and not (
                    original_block["experiment"] == "E4" and original_block["variant"] == "pilot"
                ):
                    continue
                retained = Path(continuation) / "blocks" / (original_block["block_id"] + ".json")
                saved = loads_exact(retained.read_bytes()) if retained.is_file() else {}
                passed = saved.get("replay_passed") is True and saved.get("block_passed") is True
                if original_block["experiment"] == "pilot":
                    development_pilots.extend(saved.get("rows", []))
                    development_replay_passed &= passed
                else:
                    paid_pilots.extend(saved.get("rows", []))
                    paid_replay_passed &= passed
            estimate = pilot_estimate(development_pilots, replay_passed=development_replay_passed)
        group_admissions = {}
        exhausted_currencies = {
            currency
            for currency in spending
            if (args.output / "budget-cutoffs" / (currency + ".json")).is_file()
        }
        for block in blocks:
            if stages and block["experiment"] not in stages | {"pilot"}:
                continue
            result_path = args.output / "blocks" / (block["block_id"] + ".json")
            if not rehearsal and block["experiment"] != "pilot" and estimate is None:
                estimate = pilot_estimate(
                    development_pilots, replay_passed=development_replay_passed
                )
                write_json(args.output / "pilot-estimate.json", estimate)
            if result_path.exists():
                saved = loads_exact(result_path.read_bytes())
                if saved.get("replay_passed") is True and saved.get("block_passed") is True:
                    if block["experiment"] == "pilot":
                        development_pilots.extend(saved["rows"])
                    if block["experiment"] == "E4" and block["variant"] == "pilot":
                        paid_pilots.extend(saved["rows"])
                    continue
            if block["provider"] != "local" and not credentials_available[block["provider"]]:
                # Availability is known before admission or any cost hold. Preserve
                # prior attempted observations/progress when a credential disappears.
                progress_path = args.output / "progress" / (block["block_id"] + ".jsonl")
                if not result_path.exists() and not progress_path.exists():
                    write_json(
                        result_path,
                        {
                            "rows": [
                                {**row, "reason": "missing_credentials"}
                                for row in planned_rows(block)
                            ],
                            "reason": "missing_credentials",
                        },
                    )
                continue
            if (
                not rehearsal
                and block["provider"] != "local"
                and block["experiment"] != "pilot"
                and not estimate["passed"]
            ):
                write_json(
                    result_path, {"rows": planned_rows(block), "reason": "R5 pilot incomplete"}
                )
                continue
            if block["experiment"] == "E4" and not rehearsal:
                try:
                    verify_e4(ROOT, ROOT / "evidence/dryrun/agentdojo/d3b/formal-gate.json")
                    ready = True
                except (ValueError, OSError):
                    ready = False
                if not ready or not (ROOT / ".tmp/agentdojo-venv/Scripts/python.exe").is_file():
                    write_json(
                        args.output / "blocks" / (block["block_id"] + ".json"),
                        {
                            "rows": planned_rows(block),
                            "reason": "E4 formal mapping or pinned apparatus gate unavailable",
                        },
                    )
                    continue
                if block["provider"] == "openai" and block["variant"] != "pilot":
                    passed = (
                        paid_replay_passed
                        and len(paid_pilots) == 4
                        and all(row["status"] == "valid_completed" for row in paid_pilots)
                    )
                    mean_cost = (
                        sum(
                            (
                                Fraction(row.get("usage_measurements", {}).get("cost", "0"))
                                for row in paid_pilots
                            ),
                            Fraction(0),
                        )
                        / 4
                    )
                    if not passed:
                        write_json(
                            args.output / "blocks" / (block["block_id"] + ".json"),
                            {
                                "rows": planned_rows(block),
                                "reason": "E4 paid pilot or remaining-budget gate",
                                "pilot_mean_cost_usd": str(mean_cost),
                            },
                        )
                        continue
            currency = (
                "USD"
                if block["provider"] == "openai"
                else "LOCAL"
                if block["provider"] == "local"
                else "CNY"
            )
            cap = Fraction(15) if currency == "CNY" else Fraction(args.episode_cap)
            try:
                profiles = {
                    row["config"]: _profile(row, lock, probe) for row in planned_rows(block)
                }
                if currency != "LOCAL" and block["experiment"] != "pilot":
                    group = groups_by_block[block["block_id"]]
                    group_key = H([unit["block_id"] for unit in group])[:24]
                    admission_path = args.output / "admissions" / (group_key + ".json")
                    if group_key not in group_admissions:
                        if admission_path.is_file():
                            group_admissions[group_key] = loads_exact(admission_path.read_bytes())
                        elif currency in exhausted_currencies:
                            group_admissions[group_key] = {
                                "admitted": False,
                                "reason": "trailing whole-block budget cutoff",
                            }
                        else:
                            mean = (
                                Fraction(0)
                                if rehearsal
                                else Fraction(estimate.get("mean_episode_cost_usd", "0"))
                            )
                            if (
                                block["experiment"] == "E4"
                                and block["variant"] != "pilot"
                                and not rehearsal
                            ):
                                mean = mean_cost
                            if currency == "CNY" and (
                                row_set == "domestic-api"
                                or estimate.get("basis") == "domestic_episode_ceiling_cny"
                            ):
                                mean = cap
                            elif currency == "CNY":
                                profile = next(iter(profiles.values()))
                                mean = (
                                    Fraction(estimate["mean_episode_tokens"])
                                    * max(
                                        Fraction(profile.get("assumed_price_in", "1")),
                                        Fraction(profile.get("assumed_price_out", "1")),
                                    )
                                    / 1_000_000
                                )
                            group_admissions[group_key] = reserve_comparison_block(
                                group, journals[currency], spending[currency], mean, safety_factor
                            )
                        write_json(admission_path, group_admissions[group_key])
                    if not group_admissions[group_key]["admitted"]:
                        exhausted_currencies.add(currency)
                        cutoff_path = args.output / "budget-cutoffs" / (currency + ".json")
                        if not cutoff_path.is_file():
                            write_json(
                                cutoff_path,
                                {"currency": currency, "comparison_block_id": group_key},
                            )
                        write_json(
                            result_path,
                            {
                                "rows": planned_rows(block),
                                "reason": "Complete case comparison budget admission",
                                "comparison_block_id": group_key,
                            },
                        )
                        continue
            except ValueError as error:
                write_json(
                    args.output / "blocks" / (block["block_id"] + ".json"),
                    {"rows": planned_rows(block), "reason": str(error)},
                )
                continue
            if block["provider"] == "local":
                try:
                    checked = ensure_local_runtime(
                        lock, block["model"], load=load_local_model, status=local_running_models
                    )
                except ValueError as error:
                    write_json(
                        result_path,
                        {
                            "rows": [
                                {**row, "reason": str(error), "error_class": "local_resource"}
                                for row in planned_rows(block)
                            ],
                            "reason": str(error),
                        },
                    )
                    continue
                write_json(
                    args.output / "local-block-checks" / (block["block_id"] + ".json"), checked
                )

            def run(row, attempt):
                folder = args.output / "episodes" / block["block_id"] / row["config"] / str(attempt)
                folder.mkdir(parents=True, exist_ok=True)
                attempt_id = f"{block['block_id']}:{row['config']}:{attempt}"
                actual_cap = (
                    cap if currency == "LOCAL" else spending[currency].begin(attempt_id, cap)
                )
                spec = {
                    "row": row,
                    "client_profile": profiles[row["config"]],
                    "episode_cap": str(actual_cap),
                    "currency": currency,
                    "cases": str(
                        (
                            ROOT / "tests/fixtures/blind-zh-sample"
                            if block["experiment"] == "pilot" or rehearsal
                            else args.cases
                        ).resolve()
                    ),
                    "replay_dir": str(folder / "recording"),
                    "result_path": str(folder / "result.json"),
                }
                write_json(folder / "spec.json", spec)
                try:
                    executable = (
                        str(ROOT / ".tmp/agentdojo-venv/Scripts/python.exe")
                        if row["experiment"] == "E4"
                        else sys.executable
                    )
                    entry = (
                        ROOT / "scripts/agentdojo_run.py"
                        if row["experiment"] == "E4"
                        else Path(__file__).resolve()
                    )
                    command = [
                        executable,
                        str(entry),
                        "--worker",
                        str(folder / "spec.json"),
                        "--output",
                        str(folder / "result.json"),
                    ]
                    if row["provider"] == "openai":
                        command.extend(["--env-file", str(env_file)])
                    elif row["provider"] != "local":
                        command.extend([
                            "--provider-env",
                            f"{row['provider']}={provider_env[row['provider']]}",
                        ])
                    completed = subprocess.run(
                        command,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=WORKER_TIMEOUT_SECONDS,
                        cwd=ROOT,
                        **domestic_worker_options(row["provider"]),
                    )
                    if completed.returncode or not (folder / "result.json").exists():
                        return record_worker_interruption(folder, row, "worker_crash")
                    result = loads_exact((folder / "result.json").read_bytes())
                    if rehearsal and currency == "LOCAL":
                        try:
                            checked = verify_local_runtime(
                                lock, local_running_models(), row["model"]
                            )
                            write_json(folder / "local-runtime.json", checked)
                        except ValueError as error:
                            write_json(
                                folder / "local-runtime.json",
                                {"verified": False, "reason": str(error)},
                            )
                            result.update(
                                status="runtime_error",
                                error_class="local_resource",
                                reason="local_runtime_verification_failed",
                            )
                            write_json(folder / "result.json", result)
                    if currency != "LOCAL" and "usage_measurements" in result:
                        spending[currency].settle(
                            attempt_id, Fraction(result["usage_measurements"]["cost"])
                        )
                    return result
                except subprocess.TimeoutExpired as error:
                    # The worker enforces the registered 600-second episode limit.
                    # This extra process grace is only for startup/final evidence sealing.
                    return record_worker_interruption(folder, row, "worker_timeout", error)
                except OSError as error:
                    return record_worker_interruption(folder, row, "worker_launch_error", error)

            def replay(rows):
                verified = True
                for row in rows:
                    folder = (
                        args.output
                        / "episodes"
                        / block["block_id"]
                        / row["config"]
                        / str(row["attempt"])
                    )
                    if not (folder / "result.json").is_file():
                        verified = False
                        continue
                    executable = (
                        str(ROOT / ".tmp/agentdojo-venv/Scripts/python.exe")
                        if block["experiment"] == "E4"
                        else sys.executable
                    )
                    entry = (
                        ROOT / "scripts/agentdojo_run.py"
                        if block["experiment"] == "E4"
                        else Path(__file__).resolve()
                    )
                    command = [
                        executable,
                        str(entry),
                        "--worker",
                        str(folder / "spec.json"),
                        "--strict",
                        "--output",
                        str(folder / "strict" / "result.json"),
                    ]
                    try:
                        original = loads_exact((folder / "result.json").read_bytes())
                        result = subprocess.run(
                            command,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            timeout=WORKER_TIMEOUT_SECONDS,
                            cwd=ROOT,
                            **domestic_worker_options(row["provider"]),
                        )
                        strict_path = folder / "strict/result.json"
                        matched = (
                            result.returncode == 0
                            and strict_path.is_file()
                            and observation_hash(replay_projection(original))
                            == observation_hash(
                                replay_projection(loads_exact(strict_path.read_bytes()))
                            )
                        )
                        if original.get("interrupted_prefix"):
                            # Checkpoints can replay an observed prefix but do not contain
                            # independently exported effects of an interrupted tool call.
                            matched = False
                    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
                        matched = False
                    verified = verified and matched
                return verified

            def recover(row, attempt):
                result = (
                    args.output
                    / "episodes"
                    / block["block_id"]
                    / row["config"]
                    / str(attempt)
                    / "result.json"
                )
                if not result.is_file():
                    if (result.parent / "episode/control.json").is_file():
                        return record_worker_interruption(
                            result.parent, row, "interrupted_in_flight"
                        )
                    return None
                observed = loads_exact(result.read_bytes())
                if currency != "LOCAL" and "usage_measurements" in observed:
                    spending[currency].settle(
                        f"{block['block_id']}:{row['config']}:{attempt}",
                        Fraction(observed["usage_measurements"]["cost"]),
                    )
                return observed

            result = execute_block(
                block,
                run,
                replay,
                progress=args.output / "progress" / (block["block_id"] + ".jsonl"),
                recover=recover,
            )
            if block["experiment"] == "pilot":
                development_pilots.extend(result["rows"])
                development_replay_passed &= result["replay_passed"] and result["block_passed"]
                write_json(
                    args.output / "pilot-estimate.json",
                    pilot_estimate(development_pilots, replay_passed=development_replay_passed),
                )
            if block["experiment"] == "E4" and block["variant"] == "pilot":
                paid_pilots.extend(result["rows"])
                paid_replay_passed &= result["replay_passed"] and result["block_passed"]
            write_json(args.output / "blocks" / (block["block_id"] + ".json"), result)
            if row_set == "rerun" and any(
                row.get("error_class") == "apparatus_defect" for row in result["attempts"]
            ):
                raise ValueError("R13 apparatus defect stop: retain partial results; require R14")
            # Failed blocks stay reviewable; independent blocks continue in registered order.
        if rehearsal:
            write_json(args.output / "summary.json", rehearsal_summary(blocks, args.output))
        else:
            report_rows = []
            for selected in blocks:
                report_path = args.output / "blocks" / (selected["block_id"] + ".json")
                report = loads_exact(report_path.read_bytes()) if report_path.is_file() else {}
                report_rows.extend(report.get("rows", planned_rows(selected)))
            write_json(args.output / "report-row-sets.json", report_row_sets(report_rows))


def rehearsal_summary(blocks, output):
    """Only aggregate development diagnostics, never experimental results."""
    from collections import Counter

    rows = []
    attempts = []
    replay_passed = True
    for block in blocks:
        path = output / "blocks" / (block["block_id"] + ".json")
        result = loads_exact(path.read_bytes()) if path.is_file() else {"rows": planned_rows(block)}
        rows.extend({**row, "block_reason": result.get("reason")} for row in result["rows"])
        attempts.extend(result.get("attempts", []))
        replay_passed &= result.get("replay_passed") is True

    def exercised(row):
        return (
            row["status"] == "valid_completed"
            and row.get("completed") is True
            and row.get("metrics", {}).get("tool_calls", 0) > 0
        )

    coverage = []
    for model, config in sorted({
        (row["model"], row["config"]) for row in rows if row["experiment"] == "rehearsal"
    }):
        selected = [
            row
            for row in rows
            if row["experiment"] == "rehearsal" and (row["model"], row["config"]) == (model, config)
        ]
        coverage.append({
            "model": model,
            "config": config,
            "valid_observations": sum(row["status"] == "valid_completed" for row in selected),
            "successful_paths": sum(exercised(row) for row in selected),
            "paths_with_tool_calls": sum(
                row.get("metrics", {}).get("tool_calls", 0) > 0 for row in selected
            ),
            "statuses": dict(Counter(row["status"] for row in selected)),
            "failure_classes": dict(
                Counter(row["error_class"] for row in selected if row.get("error_class"))
            ),
            "incomplete_reasons": dict(
                Counter(
                    row.get("reason")
                    or row.get("termination")
                    or row.get("block_reason")
                    or row.get("error_class")
                    or "model_did_not_complete_with_tool_effect"
                    for row in selected
                    if not exercised(row)
                )
            ),
        })
    providers = []
    for provider in sorted({row["provider"] for row in rows}):
        selected = [row for row in rows if row["provider"] == provider]
        measured = [row for row in attempts if row["provider"] == provider]
        requests = sum(
            row.get("usage_measurements", {}).get("model_requests", 0) for row in measured
        )
        tokens = sum(row.get("usage_measurements", {}).get("output_tokens", 0) for row in measured)
        failures = sum(row.get("error_class") == "model_output_format" for row in selected)
        providers.append({
            "provider": provider,
            "episodes": len(selected),
            "format_failures": failures,
            "format_failure_rate": str(Fraction(failures, len(selected))),
            "model_requests": requests,
            "mean_output_tokens_per_request": str(Fraction(tokens, requests)) if requests else None,
            "finish_reason_length": sum(
                row.get("usage_measurements", {}).get("finish_reason_length", 0) for row in measured
            ),
        })
    return {
        "rehearsal": True,
        "in_denominator": False,
        "planned": len(rows),
        "statuses": dict(Counter(row["status"] for row in rows)),
        "apparatus_defect": sum(row.get("error_class") == "apparatus_defect" for row in rows),
        "finish_reason_length": sum(row["finish_reason_length"] for row in providers),
        "strict_replay_passed": replay_passed,
        "providers": providers,
        "coverage": coverage,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", type=Path, default=ROOT / "data/blind-zh")
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--rehearsal",
        action="store_true",
        help="Development samples only; excludes all formal denominators",
    )
    parser.add_argument("--execute", action="store_true")
    parser.add_argument(
        "--domestic-api",
        action="append",
        help="Repeat: deepseek, deepseek-chat alias, qwen-plus, zhipu or exact probed model",
    )
    parser.add_argument(
        "--row-set", choices=["all", "domestic-api", "cut-continuation", "rerun"], default="all"
    )
    parser.add_argument("--continuation-of", type=Path)
    parser.add_argument("--rerun-manifest", type=Path)
    parser.add_argument("--rerun-proof", type=Path)
    parser.add_argument(
        "--prove-source",
        type=Path,
        help="Independently replay every source episode offline; --output is the proof JSON",
    )
    parser.add_argument(
        "--episode-cap", default="2", help="Loose per-episode safety valve; not used for admission"
    )
    parser.add_argument(
        "--safety-factor", default="3/2", help="Multiplier on measured pilot mean cost"
    )
    parser.add_argument(
        "--stages", default="", help="Optional comma-separated stages to resume, e.g. E2,E3"
    )
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--provider-env", action="append", default=[], metavar="PROVIDER=PATH")
    parser.add_argument("--worker", type=Path)
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()
    if args.output is None:
        args.output = ROOT / ("evidence/rehearsal" if args.rehearsal else "evidence/live-zh")
    os.chdir(ROOT)
    if args.prove_source:
        proof = prove_source_replay(args.prove_source, args.output)
        print({key: proof[key] for key in ("replayed", "consistent", "inconsistent", "passed")})
        raise SystemExit(0 if proof["passed"] else 1)
    if args.worker:
        spec = loads_exact(args.worker.read_bytes())
        if args.strict:
            with network_disabled():
                worker(
                    spec,
                    args.output,
                    "strict",
                    env_file=args.env_file,
                    provider_env=args.provider_env,
                )
        else:
            worker(
                spec, args.output, "record", env_file=args.env_file, provider_env=args.provider_env
            )
    else:
        coordinate(args)
