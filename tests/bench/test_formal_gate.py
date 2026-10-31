"""Formal preflight rejects stale evidence without inspecting case semantics."""

from copy import deepcopy
from hashlib import sha256
from pathlib import Path

import pytest
from typer.testing import CliRunner

from corridor.bench import formal_gate
from corridor.crypto.jcs import jcs_bytes


def probe_row(provider, model):
    effort = "none" if provider == "local" else "low"
    profile = {
        "model": model,
        "reasoning_effort": effort,
        "max_output_tokens": 512 if provider == "local" else 128,
        "temperature": "provider-default",
        "context_window": 32768 if provider == "local" else "provider-managed",
        "tool_budget": 1,
    }
    request = {"model": model, "content_hash": "a" * 64}
    if provider == "local":
        request.update(reasoning_effort=effort, max_tokens=512, seed=1)
    else:
        request.update(reasoning={"effort": effort}, max_output_tokens=128)
    from corridor.bench.request_probe import classify_parameters

    return {
        "model": model,
        "status": "valid_completed",
        "strict_replay": True,
        "actual_outbound_calls": 1,
        "actual_request": request,
        "deployment_profile": profile,
        "returned_model": model,
        "context_identity_matches_engine": True,
        "observed_context_window": 32768,
        "parameters": classify_parameters(
            request, api="chat" if provider == "local" else "responses"
        ),
        "request_identity": {
            "model": model,
            "returned_model": model,
            "source": "live-recorded",
            "api": "chat" if provider == "local" else "responses",
            "reasoning_effort": effort,
            "max_output_tokens": profile["max_output_tokens"],
            "sampling": {
                "seed": 1 if provider == "local" else None,
                "temperature": "provider-default",
            },
            "outbound_request_hash": "a" * 64,
            "deployment_profile": profile,
        },
    }


def ready_probe():
    return {
        "providers": {
            provider: {
                "gate_passed": True,
                "models": [probe_row(provider, model) for model in sorted(models)],
            }
            for provider, models in formal_gate.REQUIRED_MODELS.items()
        }
    }


def apparatus_fixture(root, path, kind, hashes=None):
    from corridor.bench.apparatus import (
        ALL_CONFIGS,
        KINDS,
        VARIANTS,
        WORLD_ROLES,
        apparatus_source_hashes,
    )

    cases = (
        ["sample-apparatus-" + goal for goal in KINDS]
        if kind == "sweep"
        else [f"synthetic-{index}" for index in range(24)]
    )
    rows = [
        {
            "case_id": case,
            "config": {
                "name": config,
                "variant": variant,
                "seed": 1,
                **({"role": role} if kind == "sweep" else {}),
            },
            "status": "observed",
            "error_class": None,
            "traceback": "",
        }
        for case in cases
        for config in ALL_CONFIGS
        for variant in VARIANTS
        for role in (WORLD_ROLES if kind == "sweep" else (None,))
    ]
    raw = b"".join(jcs_bytes(row) + b"\n" for row in rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    (path.parent / "diagnostics.jsonl").write_bytes(raw)
    value = {
        "schema_version": "apparatus-check-v1",
        "kind": kind,
        "complete": True,
        "passed": True,
        "source_stable": True,
        "apparatus_defects": 0,
        "model_output_formats": 0,
        "source_hashes": apparatus_source_hashes(root) if hashes is None else hashes,
        "expected_cells": len(rows),
        "observed_cells": len(rows),
        "diagnostics_sha256": sha256(raw).hexdigest(),
    }
    if kind == "c2_blackbox":
        manifest = root / "data/blind-zh/FROZEN-SHA256.txt"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        if not manifest.is_file():
            manifest.write_bytes(b"synthetic frozen manifest metadata\n")
        value["input_binding"] = {
            "corpus_identity": "data/blind-zh/cases",
            "frozen_manifest_sha256": sha256(manifest.read_bytes()).hexdigest(),
        }
        value["input_stable"] = True
    path.write_bytes(jcs_bytes(value))
    return value


@pytest.mark.parametrize(
    "field,value",
    [
        ("request_model", "another-model"),
        ("identity_model", "another-model"),
        ("returned_model", "another-model"),
        ("request_reasoning", "none"),
        ("profile_reasoning", "none"),
        ("identity_reasoning", "none"),
        ("request_max_tokens", 1024),
        ("identity_api", "chat"),
        ("identity_source", "scripted"),
        ("parameter_sent", False),
    ],
)
def test_probe_rejects_stale_wire_or_identity(field, value):
    document = ready_probe()
    row = document["providers"]["openai"]["models"][0]
    if field == "request_model":
        row["actual_request"]["model"] = value
    elif field == "identity_model":
        row["request_identity"]["model"] = value
    elif field == "returned_model":
        row["returned_model"] = value
    elif field == "request_reasoning":
        row["actual_request"]["reasoning"]["effort"] = value
    elif field == "profile_reasoning":
        row["deployment_profile"]["reasoning_effort"] = value
    elif field == "identity_reasoning":
        row["request_identity"]["reasoning_effort"] = value
    elif field == "request_max_tokens":
        row["actual_request"]["max_output_tokens"] = value
    elif field == "identity_api":
        row["request_identity"]["api"] = value
    elif field == "identity_source":
        row["request_identity"]["source"] = value
    else:
        row["parameters"]["reasoning_effort"]["sent"] = value
    with pytest.raises(ValueError, match="probe|reasoning|deployment"):
        formal_gate.verify_probe(document, "openai")


def test_model_lock_compares_profiles_instead_of_summary_flag(monkeypatch, tmp_path):
    document = ready_probe()
    profiles = [
        deepcopy(row["deployment_profile"]) for row in document["providers"]["local"]["models"]
    ]
    monkeypatch.setattr(formal_gate, "local_deployments", lambda path: profiles)
    formal_gate.verify_model_lock(document, tmp_path / "lock.json")
    profiles[0]["context_window"] = 4096
    with pytest.raises(ValueError, match="differs"):
        formal_gate.verify_model_lock(document, tmp_path / "lock.json")


def test_local_gate_rejects_consistently_stale_8192_context(monkeypatch, tmp_path):
    document = ready_probe()
    for row in document["providers"]["local"]["models"]:
        row["deployment_profile"]["context_window"] = 8192
        row["request_identity"]["deployment_profile"]["context_window"] = 8192
        row["observed_context_window"] = 8192
    with pytest.raises(ValueError, match="context"):
        formal_gate.verify_probe(document, "local")
    profiles = [row["deployment_profile"] for row in document["providers"]["local"]["models"]]
    monkeypatch.setattr(formal_gate, "local_deployments", lambda path: profiles)
    with pytest.raises(ValueError, match="context"):
        formal_gate.verify_model_lock(document, tmp_path / "lock.json")


def test_operator_can_skip_blind_access_without_claiming_readiness(tmp_path, monkeypatch):
    def forbidden(*args):
        pytest.fail("Blind files must not be opened under the no-access option")

    monkeypatch.setattr(formal_gate, "verify_frozen_hashes", forbidden)
    monkeypatch.setattr(formal_gate, "verify_strict_cases", forbidden)
    result = formal_gate.run_formal_gate(root=tmp_path, blind_access=False)
    assert result["ready"] is False
    selected = [row for row in result["checks"] if row["name"].startswith("blind_")]
    assert len(selected) == 2 and all(not row["ready"] for row in selected)
    assert all("disabled" in row["reason"] for row in selected)


def test_probe_rechecks_recorded_artifact_and_rejects_stale_output(tmp_path):
    from corridor.agent.llm import LLMClient
    from corridor.bench.request_probe import _call
    from corridor.crypto.jcs import H

    document = ready_probe()
    row = document["providers"]["openai"]["models"][0]
    client = LLMClient(
        mode="scripted",
        model=row["model"],
        config="D3-probe",
        api="responses",
        reasoning_effort="low",
        max_output_tokens=128,
        deployment_profile=row["deployment_profile"],
        env_file=None,
    )
    result = _call(client)
    trace = client.calls[0]
    row["request_identity"].update(trace, source="live-recorded")
    request = {
        key: value
        for key, value in trace.items()
        if key not in {"source", "request_hash", "replay_key"}
    }
    replay = (
        tmp_path
        / "replay/openai/0"
        / result.replay_key[:16]
        / f"{request['prompt_hash'][:16]}.json"
    )
    replay.parent.mkdir(parents=True)
    record = {
        "source": "live-recorded",
        "request": request,
        "replay_key": result.replay_key,
        "output": result.output,
        "output_hash": H(result.output),
    }
    replay.write_bytes(jcs_bytes(record))
    formal_gate.verify_probe_replays(document, "openai", tmp_path)
    record["output"] = {"handle": "CHANGED"}
    replay.write_bytes(jcs_bytes(record))
    with pytest.raises(ValueError, match="replay"):
        formal_gate.verify_probe_replays(document, "openai", tmp_path)


def frozen_fixture(tmp_path):
    cases = tmp_path / "cases"
    cases.mkdir()
    # Deliberately invalid JSON: this gate must only hash opaque bytes.
    payload = b"opaque, never parse this as JSON"
    (cases / "one.json").write_bytes(payload)
    manifest = tmp_path / "FROZEN-SHA256.txt"
    manifest.write_text(f"{sha256(payload).hexdigest()}  cases/one.json\n", encoding="utf-8")
    return cases, manifest


def test_frozen_cases_are_opaque_and_exact(tmp_path):
    cases, manifest = frozen_fixture(tmp_path)
    formal_gate.verify_frozen_hashes(cases, manifest)
    (cases / "one.json").write_bytes(b"changed")
    with pytest.raises(ValueError, match="digest"):
        formal_gate.verify_frozen_hashes(cases, manifest)


@pytest.mark.parametrize("name", ["../escape.json", "/escape.json", "C:/escape.json"])
def test_manifest_cannot_escape_cases(tmp_path, name):
    cases, manifest = frozen_fixture(tmp_path)
    manifest.write_text(f"{'a' * 64}  {name}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="path"):
        formal_gate.verify_frozen_hashes(cases, manifest)


def test_empty_duplicate_missing_and_unlisted_cases_fail(tmp_path):
    cases, manifest = frozen_fixture(tmp_path)
    original = manifest.read_text(encoding="utf-8")
    for value in ("", original + original):
        manifest.write_text(value, encoding="utf-8")
        with pytest.raises(ValueError):
            formal_gate.verify_frozen_hashes(cases, manifest)
    manifest.write_text(original, encoding="utf-8")
    (cases / "extra.json").write_bytes(b"unlisted")
    with pytest.raises(ValueError, match="file set"):
        formal_gate.verify_frozen_hashes(cases, manifest)
    (cases / "extra.json").unlink()
    (cases / "one.json").unlink()
    with pytest.raises(ValueError, match="file set"):
        formal_gate.verify_frozen_hashes(cases, manifest)


def test_probe_does_not_trust_summary_boolean():
    with pytest.raises(ValueError, match="probe"):
        formal_gate.verify_probe({"providers": {"openai": {"gate_passed": True}}}, "openai")


def test_all_checks_report_missing_evidence_independently(tmp_path, monkeypatch):
    monkeypatch.setattr(formal_gate, "workspace_clean", lambda root: None)
    monkeypatch.setattr(formal_gate, "local_deployments", lambda path: [])
    result = formal_gate.run_formal_gate(root=tmp_path)
    assert result["status"] == "NOT READY"
    assert len(result["checks"]) == 13
    assert next(row for row in result["checks"] if row["name"] == "workspace")["ready"]
    assert not next(row for row in result["checks"] if row["name"] == "blind_hashes")["ready"]


def test_cli_reports_not_ready_and_nonzero(monkeypatch, tmp_path):
    from corridor.cli import app
    from corridor.cli import formal_gate as cli

    report = {
        "status": "NOT READY",
        "ready": False,
        "checks": [{"name": "workspace", "ready": False, "reason": "workspace is dirty"}],
    }
    monkeypatch.setattr(cli, "run_formal_gate", lambda **kw: report)
    output = tmp_path / "gate.json"
    result = CliRunner().invoke(app, ["formal-gate", "--output", str(output)])
    assert result.exit_code == 1
    assert "NOT READY" in result.stdout and "workspace is dirty" in result.stdout
    assert output.read_bytes() == jcs_bytes(report)


def e4_fixture(root):
    from corridor.bench.agentdojo_contract import (
        BENCHMARK_VERSION,
        COMMIT,
        E4_GATE_ARTIFACT_FILES,
        E4_GATE_CONDITIONS,
        E4_GATE_SOURCE_FILES,
        digest,
    )

    def write(name, value):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(jcs_bytes(value))
        return sha256(path.read_bytes()).hexdigest()

    sources = {name: write(name, {"synthetic_source": name}) for name in E4_GATE_SOURCE_FILES}
    pairs = [(f"user_task_{u}", f"injection_task_{i}") for u in range(16) for i in range(9)]
    cells = [
        ("A_null", "prompt-bound"),
        ("A0", "prompt-bound"),
        ("A5", "prompt-bound"),
        ("A5", "auto-deny"),
        ("A5", "auto-approve"),
    ]
    gt_rows = [
        {
            "config": config,
            "approval_mode": approval,
            "user_task": user,
            "injection_task": injection,
            "status": "valid_completed",
            "variant": "attack",
            "model_independent": True,
            "model_calls": 0,
        }
        for config, approval in cells
        for user, injection in pairs
    ]
    episodes = [(user, injection, "attack") for user, injection in pairs]
    episodes += [(f"user_task_{u}", None, "benign") for u in range(16)]
    episodes += [(f"injection_task_{i}", None, "sanity") for i in range(9)]
    noop_rows = [
        {
            "config": config,
            "approval_mode": "prompt-bound",
            "user_task": user,
            "injection_task": injection,
            "status": "valid_completed",
            "variant": variant,
        }
        for config in ("A_null", "A0", "A5")
        for user, injection, variant in episodes
    ]
    artifacts = {
        E4_GATE_ARTIFACT_FILES[0]: {
            "calibration_passed": True,
            "commit": COMMIT,
            "benchmark_version": BENCHMARK_VERSION,
        },
        E4_GATE_ARTIFACT_FILES[1]: [{"passed": True} for _ in range(9)],
        E4_GATE_ARTIFACT_FILES[2]: [
            {"function": function, "kind": kind, "passed": True}
            for function in (
                "send_money",
                "schedule_transaction",
                "update_scheduled_transaction",
                "update_password",
                "update_user_info",
            )
            for kind in (
                ("negative",)
                if function == "update_user_info"
                else ("positive", "deny", "negative")
            )
        ],
        E4_GATE_ARTIFACT_FILES[3]: {
            "passed": True,
            "real_model_calls": 0,
            "rows": len(noop_rows),
            "variants_per_config": {
                config: {"benign": 16, "attack": 144, "sanity": 9}
                for config in ("A_null", "A0", "A5")
            },
        },
        E4_GATE_ARTIFACT_FILES[4]: noop_rows,
        E4_GATE_ARTIFACT_FILES[5]: {
            "schema_version": "agentdojo-groundtruth-v1",
            "formal_result": True,
            "model_independent": True,
            "model_calls": 0,
            "commit": COMMIT,
            "benchmark_version": BENCHMARK_VERSION,
            "pairs_per_config": 144,
            "deterministic_replay_passed": True,
            "rows": len(gt_rows),
            "first_run_digest": digest(gt_rows),
            "repeat_run_digest": digest(gt_rows),
            "runtime_errors": 0,
            "not_run": 0,
            "configs": [
                {"config": config, "approval_mode": approval, "attack_n": 144}
                for config, approval in cells
            ],
        },
        E4_GATE_ARTIFACT_FILES[6]: gt_rows,
    }
    hashes = {name: write(name, value) for name, value in artifacts.items()}
    artifacts[E4_GATE_ARTIFACT_FILES[5]]["rows_sha256"] = hashes[E4_GATE_ARTIFACT_FILES[6]]
    hashes[E4_GATE_ARTIFACT_FILES[5]] = write(
        E4_GATE_ARTIFACT_FILES[5], artifacts[E4_GATE_ARTIFACT_FILES[5]]
    )
    gate = {
        "formal_e4_gate": True,
        "conditions": {name: True for name in E4_GATE_CONDITIONS},
        "source_hashes": sources,
        "artifacts": hashes,
    }
    path = root / "evidence/dryrun/agentdojo/d3b/formal-gate.json"
    write(path.relative_to(root), gate)
    return path, gate, artifacts, write


@pytest.mark.parametrize(
    "mutation", ["missing_condition", "missing_source", "stale_source", "incomplete_rows"]
)
def test_e4_rejects_stale_or_incomplete_underlying_evidence(tmp_path, mutation):
    path, gate, artifacts, write = e4_fixture(tmp_path)
    formal_gate.verify_e4(tmp_path, path)
    if mutation == "missing_condition":
        del gate["conditions"][next(iter(gate["conditions"]))]
    elif mutation == "missing_source":
        del gate["source_hashes"][next(iter(gate["source_hashes"]))]
    elif mutation == "stale_source":
        write(next(iter(gate["source_hashes"])), {"changed": True})
    else:
        name = "results/e4-groundtruth/rows.json"
        rows = artifacts[name][:-1]
        gate["artifacts"][name] = write(name, rows)
    write(path.relative_to(tmp_path), gate)
    with pytest.raises(ValueError):
        formal_gate.verify_e4(tmp_path, path)


def test_source_manifest_cannot_read_opaque_case_content(tmp_path):
    cases, _ = frozen_fixture(tmp_path)
    with pytest.raises(ValueError, match="case paths"):
        formal_gate.verify_source_hashes(tmp_path, {"CaSeS/one.json": "a" * 64})
    with pytest.raises(ValueError, match="case paths"):
        formal_gate._load(cases / "one.json")


def test_complete_development_fixture_is_ready_without_model_calls(tmp_path, monkeypatch):
    from corridor.agent.llm import LLMClient
    from corridor.bench.ablation import check_inventory
    from corridor.bench.blind_zh import load_cases
    from corridor.bench.request_probe import _call
    from corridor.crypto.jcs import H

    e4, gate, _artifacts, write = e4_fixture(tmp_path)
    frozen_root = tmp_path / "data/blind-zh"
    frozen_root.mkdir(parents=True)
    cases, manifest = frozen_fixture(frozen_root)
    payload = jcs_bytes(load_cases(Path("tests/fixtures/blind-zh-sample"))[0])
    (cases / "one.json").write_bytes(payload)
    manifest.write_text(f"{sha256(payload).hexdigest()}  cases/one.json\n", encoding="utf-8")
    for source in formal_gate.witness_source_hashes():
        if source not in gate["source_hashes"]:
            gate["source_hashes"][source] = write(source, {"synthetic_source": source})
    write(e4.relative_to(tmp_path), gate)
    operations, coverage = [], {}
    for kind in formal_gate.OPERATION_CATEGORIES:
        readonly = kind in formal_gate.READ_OPERATIONS
        configs = {}
        for config in formal_gate.CONFIGS:
            configs[config] = {
                "evaluated": 0 if readonly else 2,
                "not_evaluable": 2 if readonly else 0,
            }
            for attack in (False, True):
                operations.append({
                    "operation": kind,
                    "config": config,
                    "attack": attack,
                    "status": "not_evaluable"
                    if readonly
                    else "rejected"
                    if attack and config == "A5"
                    else "settled",
                    "input_hash": "a" * 64,
                    "oracle": {
                        "completed": not attack,
                        "prohibited_effect": attack and config == "A3",
                    },
                })
        coverage[kind] = {
            "evaluated": 0 if readonly else 2 * len(formal_gate.CONFIGS),
            "not_evaluable": 2 * len(formal_gate.CONFIGS) if readonly else 0,
            "reason": "read-only goal" if readonly else None,
            "configs": configs,
            "positive_witness_passed": not readonly,
            "negative_witness_passed": not readonly,
        }
    write(
        "evidence/domestic/d3b/witness/operation-matrix.json",
        {
            "passed": True,
            "coverage": coverage,
            "rows": operations,
        },
    )
    witness = {
        "passed": True,
        "manifest_errors": [],
        "witnesses": [
            {"passed": True, "witness_class": kind, "input_hash": "a" * 64} for kind in range(1, 6)
        ]
        + [
            {
                "passed": True,
                "witness_class": 4,
                "input_hash": "a" * 64,
                "witness_id": "reachable-" + predicate,
                "actual": {
                    "weak": {
                        "config": "A0",
                        "status": "valid_completed",
                        "input_hash": "b" * 64,
                        "harm": {category: True},
                    },
                    "strong": {
                        "config": "A5",
                        "status": "valid_completed",
                        "input_hash": "b" * 64,
                        "harm": {category: False},
                        "prohibited_effect": False,
                    },
                },
            }
            for predicate, category in (
                ("canary_exposed", "privacy"),
                ("unsupported_claim", "false_claim"),
            )
        ],
        "c1_existing": [{"legacy_result": {"passed": True}} for _ in range(40)],
        "c1_additions": [{"mechanism_blocked": True} for _ in range(8)],
        "source_hashes": gate["source_hashes"],
        "operation_witnesses_passed": True,
        "operation_coverage": coverage,
    }
    write("evidence/domestic/d3b/witness/witness.json", witness)
    write("bench/ablation_manifest.json", {"checks": check_inventory()})
    document = ready_probe()
    evidence = tmp_path / "evidence/probe"
    for provider, group in document["providers"].items():
        for index, row in enumerate(group["models"]):
            client = LLMClient(
                mode="scripted",
                model=row["model"],
                config="D3-probe",
                api="chat" if provider == "local" else "responses",
                provider="ollama" if provider == "local" else "openai",
                api_base="http://127.0.0.1:11434/v1"
                if provider == "local"
                else "https://api.openai.com/v1",
                reasoning_effort=row["deployment_profile"]["reasoning_effort"],
                seed=1 if provider == "local" else None,
                max_output_tokens=row["deployment_profile"]["max_output_tokens"],
                deployment_profile=row["deployment_profile"],
                env_file=None,
            )
            result = _call(client)
            trace = client.calls[0]
            row["request_identity"].update(trace, source="live-recorded")
            request = {
                key: value
                for key, value in trace.items()
                if key not in {"source", "request_hash", "replay_key"}
            }
            replay = (
                evidence
                / "replay"
                / provider
                / str(index)
                / result.replay_key[:16]
                / f"{request['prompt_hash'][:16]}.json"
            )
            write(
                replay.relative_to(tmp_path),
                {
                    "source": "live-recorded",
                    "request": request,
                    "replay_key": result.replay_key,
                    "output": result.output,
                    "output_hash": H(result.output),
                },
            )
    write("evidence/probe/requests.json", document)
    mini = probe_row("openai", "gpt-5.4-mini")
    mini_document = {"providers": {"openai": {"gate_passed": True, "models": [mini]}}}
    mini_client = LLMClient(
        mode="scripted",
        model=mini["model"],
        config="FIX-L3-mini-probe",
        api="responses",
        reasoning_effort="low",
        max_output_tokens=128,
        deployment_profile=mini["deployment_profile"],
        env_file=None,
    )
    mini_result = _call(mini_client)
    mini_trace = mini_client.calls[0]
    mini["request_identity"].update(mini_trace, source="live-recorded")
    mini_request = {
        key: value
        for key, value in mini_trace.items()
        if key not in {"source", "request_hash", "replay_key"}
    }
    write("evidence/fix-l3/mini-probe/requests.json", mini_document)
    write(
        f"evidence/fix-l3/mini-probe/replay/{mini_result.replay_key[:16]}/{mini_request['prompt_hash'][:16]}.json",
        {
            "source": "live-recorded",
            "request": mini_request,
            "replay_key": mini_result.replay_key,
            "output": mini_result.output,
            "output_hash": H(mini_result.output),
        },
    )
    for folder, kind in (("apparatus-sweep", "sweep"), ("apparatus-dryrun", "c2_blackbox")):
        apparatus_fixture(tmp_path, tmp_path / f"evidence/{folder}/summary.json", kind)
    from corridor.bench.apparatus import apparatus_source_hashes
    from corridor.bench.exact_goal_witness import AUTH_CONFIGS, NOT_EVALUABLE_REASONS

    expected = 24 * 2 * len(AUTH_CONFIGS)
    write(
        "evidence/exact-goal-witness/summary.json",
        {
            "schema_version": "exact-goal-witness-v2",
            "gate_basis": "exact_goal_approval_with_complete_business_accounting",
            "passed": True,
            "complete": True,
            "source_stable": True,
            "input_stable": True,
            "expected_cells": expected,
            "observed_cells": expected,
            "configs": list(AUTH_CONFIGS),
            "allowed_not_evaluable_reasons": sorted(NOT_EVALUABLE_REASONS),
            "counts": {
                "passed": expected,
                "approvals": expected,
                "approved": expected,
                "completed": expected,
                "not_evaluable": 0,
                "missing_approval_observations": 0,
            },
            "failures": {},
            "cells": [
                {
                    "cell_index": i,
                    "passed": True,
                    "approvals": 1,
                    "approved": 1,
                    "completed": True,
                    "not_evaluable": False,
                    "missing_approval_observations": 0,
                    "business_status": "valid_completed",
                    "business_error_class": None,
                }
                for i in range(expected)
            ],
            "business_outcomes": {
                "completed": expected,
                "not_completed": 0,
                "statuses": {"valid_completed": expected},
                "error_classes": {"none": expected},
            },
            "not_evaluable_reasons": {},
            "input_binding": {
                "corpus_identity": "data/blind-zh/cases",
                "frozen_manifest_sha256": sha256(manifest.read_bytes()).hexdigest(),
            },
            "source_hashes": apparatus_source_hashes(tmp_path),
        },
    )
    profiles = [
        deepcopy(row["deployment_profile"]) for row in document["providers"]["local"]["models"]
    ]
    monkeypatch.setattr(formal_gate, "local_deployments", lambda path: profiles)
    monkeypatch.setattr(formal_gate, "workspace_clean", lambda root: None)
    report = formal_gate.run_formal_gate(
        root=tmp_path, cases=cases, frozen_manifest=manifest, e4=e4
    )
    assert report["status"] == "READY", report
    assert report["ready"] is True
    assert len(report["checks"]) == 13
    assert all(row["ready"] for row in report["checks"])
    assert report["formal_model_calls"] == 0
    next(row for row in operations if row["config"] == "A5" and not row["attack"])["oracle"][
        "completed"
    ] = False
    write(
        "evidence/domestic/d3b/witness/operation-matrix.json",
        {
            "passed": True,
            "coverage": coverage,
            "rows": operations,
        },
    )
    with pytest.raises(ValueError, match="positive/negative"):
        formal_gate.verify_witness(
            tmp_path, tmp_path / "evidence/domestic/d3b/witness/witness.json"
        )
