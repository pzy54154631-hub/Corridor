"""Synthetic apparatus checks must never persist business observations."""

from copy import deepcopy
from hashlib import sha256

from corridor.bench import apparatus
from corridor.bench.blind_zh import validate_case
from corridor.schemas.base import loads_exact


def test_blackbox_binding_uses_only_canonical_identity_and_manifest_bytes(tmp_path):
    manifest = tmp_path / "data/blind-zh/FROZEN-SHA256.txt"
    manifest.parent.mkdir(parents=True)
    manifest.write_bytes(b"synthetic public manifest\n")
    expected = {
        "corpus_identity": "data/blind-zh/cases",
        "frozen_manifest_sha256": sha256(manifest.read_bytes()).hexdigest(),
    }
    # No cases directory or case contents exist in this test.
    assert apparatus.blackbox_input_binding(manifest.parent / "cases", root=tmp_path) == expected
    assert apparatus.blackbox_input_binding(manifest.parent, root=tmp_path) == expected
    assert apparatus.blackbox_input_binding(tmp_path / "other", root=tmp_path) == {
        "corpus_identity": "unregistered",
        "frozen_manifest_sha256": None,
    }


def test_blackbox_evidence_fails_if_input_binding_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(apparatus, "apparatus_source_hashes", lambda **kw: {"code": "hash"})
    summary = apparatus._write_run(
        [],
        tmp_path,
        kind="c2_blackbox",
        expected=0,
        input_binding={"corpus_identity": "data/blind-zh/cases", "frozen_manifest_sha256": "old"},
        input_probe=lambda: {
            "corpus_identity": "data/blind-zh/cases",
            "frozen_manifest_sha256": "changed",
        },
    )
    assert not summary["input_stable"]
    assert not summary["complete"] and not summary["passed"]


def test_sweep_covers_every_declared_kind_role_config_and_both_variants():
    cells = list(apparatus.sweep_cells())
    expected = {
        (config, kind, role, variant)
        for config in apparatus.ALL_CONFIGS
        for kind in apparatus.KINDS
        for role in apparatus.WORLD_ROLES
        for variant in apparatus.VARIANTS
    }
    assert {(c, case["legit_goal"]["kind"], role, v) for c, case, role, v in cells} == expected
    for _, case, _, _ in cells:
        validate_case(case)


def test_sanitizer_is_an_allowlist_and_removes_outcomes_and_messages():
    row = {
        "status": "valid_completed",
        "completed": True,
        "harm": {"private": "SENSITIVE"},
        "prohibited_effect": True,
        "episode": {"secret": "SENSITIVE"},
        "steps": [{"status": "settled"}],
        "traceback": "SENSITIVE",
        "terminal_error": {"message": "SENSITIVE"},
    }
    safe = apparatus.diagnostic(row, case_id="sample-check", config="A5", variant="attack")
    assert set(safe) == {"case_id", "config", "status", "error_class", "traceback"}
    assert safe["status"] == "observed"
    assert "SENSITIVE" not in str(safe)
    assert "completed" not in str(safe)


def test_traceback_contains_code_frames_only():
    row = {
        "status": "runtime_error",
        "error_class": "apparatus_defect",
        "traceback": (
            "Traceback (most recent call last):\n"
            '  File "<repo>/src/corridor/bench/blind_zh.py", line 12, in run_case\n'
            "ValueError: SENSITIVE\n"
            '  File "<repo>/data/blind-zh/cases/SENSITIVE.json", line 1, in secret\n'
        ),
    }
    safe = apparatus.diagnostic(row, case_id="sample-check", config="A5", variant="attack")
    assert safe["error_class"] == "apparatus_defect"
    assert "blind_zh.py" in safe["traceback"]
    assert "SENSITIVE" not in safe["traceback"]


def test_blackbox_never_passes_evidence_directory_to_runner(tmp_path, monkeypatch):
    case = apparatus.synthetic_case("transfer")
    monkeypatch.setattr(apparatus, "load_cases", lambda path: [deepcopy(case)])
    monkeypatch.setattr(apparatus, "apparatus_source_hashes", lambda **kw: {"code": "hash"})
    monkeypatch.setattr(apparatus, "blackbox_input_binding", lambda path: {"synthetic": "hash"})
    calls = []

    def fake(case, **kwargs):
        assert kwargs.get("evidence_dir") is None
        assert kwargs["client"].mode == "scripted"
        calls.append(kwargs)
        return {"status": "valid_completed", "prohibited_effect": True, "harm": "SENSITIVE"}

    monkeypatch.setattr(apparatus, "run_case", fake)
    summary = apparatus.run_blackbox(tmp_path / "unread", tmp_path / "out", expected_cases=1)
    assert summary["complete"] and summary["passed"]
    assert len(calls) == len(apparatus.ALL_CONFIGS) * 2
    for path in (tmp_path / "out").iterdir():
        text = path.read_text(encoding="utf-8")
        assert "SENSITIVE" not in text and "prohibited_effect" not in text
    stored = loads_exact((tmp_path / "out" / "summary.json").read_bytes())
    assert stored["apparatus_defects"] == 0


def test_blackbox_defects_fail_without_exception_message(tmp_path, monkeypatch):
    monkeypatch.setattr(
        apparatus, "load_cases", lambda path: [apparatus.synthetic_case("transfer")]
    )
    monkeypatch.setattr(apparatus, "apparatus_source_hashes", lambda **kw: {"code": "hash"})
    monkeypatch.setattr(apparatus, "blackbox_input_binding", lambda path: {"synthetic": "hash"})

    def broken(*args, **kwargs):
        raise ValueError("SENSITIVE")

    monkeypatch.setattr(apparatus, "run_case", broken)
    summary = apparatus.run_blackbox(tmp_path / "unread", tmp_path / "out", expected_cases=1)
    assert summary["complete"] and not summary["passed"]
    assert summary["apparatus_defects"] == len(apparatus.ALL_CONFIGS) * 2
    assert "SENSITIVE" not in (tmp_path / "out" / "diagnostics.jsonl").read_text(encoding="utf-8")
