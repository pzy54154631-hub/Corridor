"""Strict validation uses only public development and synthetic cases."""

from copy import deepcopy
from pathlib import Path

import pytest

from corridor.agent.llm import LLMClient
from corridor.bench import blind_zh, formal_gate
from corridor.bench.domestic_metrics import aggregate_runs
from corridor.crypto.jcs import jcs_bytes

SAMPLES = Path("tests/fixtures/blind-zh-sample")


@pytest.mark.parametrize(
    "bad",
    [
        b'{"case_id":"PRIVATE_SENTINEL","case_id":"duplicate"}',
        b'{"nested":{"value":1.5}}',
        b'{"nested":{"value":NaN}}',
        b"not JSON PRIVATE_SENTINEL",
        b"[]",
    ],
)
def test_invalid_json_is_retained_without_content_or_model_calls(tmp_path, bad):
    cases = tmp_path / "cases"
    cases.mkdir()
    (cases / "bad-01.json").write_bytes(bad)
    assert blind_zh.validate_cases(cases) == [{"file_id": "bad-01", "passed": False}]
    client = LLMClient()
    report = blind_zh.run_blind_zh(cases, tmp_path / "output", client=client)
    assert report["planned"] == report["invalid_case"] == 2
    assert report["runtime_errors"] == report["valid_completed"] == 0
    assert client.calls == []
    for row in report["results"]:
        assert row["status"] == "invalid_case"
        assert row["completed"] is False and row["prohibited_effect"] is None
    assert all(
        b"PRIVATE_SENTINEL" not in path.read_bytes() for path in (tmp_path / "output").rglob("*.*")
    )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda case: case.update(attacker_entry=""),
        lambda case: case.update(extra="PRIVATE_SENTINEL"),
        lambda case: case["legit_goal"].update(allow_mfa=1),
        lambda case: case["turns"][0].update(user_text=""),
        lambda case: case["turns"][0].update(external_documents=[{"kind": "text"}]),
        lambda case: case["legit_twin"].update(turns=case["turns"] * 9),
        lambda case: case.update(
            faults=[{"kind": "bank_timeout", "turn": True, "expectation": "must_stop"}]
        ),
    ],
)
def test_author_schema_constraints_are_used_by_validation_and_runner(tmp_path, mutate):
    case = deepcopy(blind_zh.load_cases(SAMPLES)[0])
    mutate(case)
    path = tmp_path / "synthetic.json"
    path.write_bytes(jcs_bytes(case))
    assert blind_zh.validate_cases(path) == [{"file_id": "synthetic", "passed": False}]
    report = blind_zh.run_blind_zh(path, tmp_path / "out")
    assert report["invalid_case"] == 2
    assert blind_zh.run_case(case, client=LLMClient())["status"] == "invalid_case"


def test_invalid_file_does_not_skip_or_abort_valid_sibling(tmp_path):
    cases = tmp_path / "cases"
    cases.mkdir()
    case = blind_zh.load_cases(SAMPLES)[0]
    (cases / "valid.json").write_bytes(jcs_bytes(case))
    (cases / "invalid.json").write_bytes(b"{}")
    report = blind_zh.run_blind_zh(cases, tmp_path / "out")
    assert report["planned"] == 4
    assert report["valid_completed"] == report["invalid_case"] == 2


def test_duplicate_case_id_invalidates_both_files_without_losing_denominator(tmp_path):
    case = blind_zh.load_cases(SAMPLES)[0]
    for name in ("one", "two"):
        (tmp_path / f"{name}.json").write_bytes(jcs_bytes(case))
    assert blind_zh.validate_cases(tmp_path) == [
        {"file_id": "one", "passed": False},
        {"file_id": "two", "passed": False},
    ]
    assert {row["case_id"] for row in blind_zh.load_cases(tmp_path)} == {"one", "two"}


@pytest.mark.parametrize(
    "extra",
    [
        '"hidden":{"same":"PRIVATE_SENTINEL","same":"other"}',
        '"hidden":0.1',
    ],
)
def test_strict_parser_checks_nested_extensible_goal_before_schema(tmp_path, extra):
    case = blind_zh.load_cases(SAMPLES)[0]
    payload = jcs_bytes(case).decode("utf-8")
    payload = payload.replace('"legit_goal":{', '"legit_goal":{' + extra + ",", 1)
    path = tmp_path / "nested.json"
    path.write_text(payload, encoding="utf-8")
    assert blind_zh.validate_cases(path) == [{"file_id": "nested", "passed": False}]


def test_invalid_filename_identity_cannot_overwrite_a_valid_case(tmp_path):
    case = deepcopy(blind_zh.load_cases(SAMPLES)[0])
    case["case_id"] = "invalid"
    (tmp_path / "valid.json").write_bytes(jcs_bytes(case))
    (tmp_path / "invalid.json").write_bytes(b"{}")
    assert blind_zh.validate_cases(tmp_path) == [
        {"file_id": "invalid", "passed": False},
        {"file_id": "valid", "passed": False},
    ]
    assert {row["case_id"] for row in blind_zh.load_cases(tmp_path)} == {"valid", "invalid"}


def test_invalid_cases_stay_unresolved_and_have_separate_report_count():
    from tests.bench.test_domestic_report import _report

    rows = [
        {
            "case_id": "invalid",
            "model": "mock",
            "config": "A5",
            "mode": "scripted",
            "seed": seed,
            "status": "invalid_case",
            "completed": False,
            "prohibited_effect": None,
            "metrics": {},
        }
        for seed in (1, 2, 3)
    ]
    result = aggregate_runs(rows)
    assert result["planned"] == result["invalid_case"] == 3
    assert result["asr_any_at_3"]["bounds"] == ["0/1", "1/1"]
    assert result["asr_any_at_3"]["interval"] is None
    text, evidence = _report().build_report(rows, dryrun=True)
    assert "|invalid_case|" in text
    assert evidence["summaries"][0]["invalid_case"] == 3


def test_formal_gate_strict_check_receives_only_pass_fail(monkeypatch, tmp_path):
    (tmp_path / "synthetic.json").write_bytes(b"opaque mock input")
    monkeypatch.setattr(
        formal_gate, "validate_cases", lambda path: [{"file_id": "synthetic", "passed": True}]
    )
    formal_gate.verify_strict_cases(tmp_path)
    monkeypatch.setattr(
        formal_gate, "validate_cases", lambda path: [{"file_id": "synthetic", "passed": False}]
    )
    with pytest.raises(ValueError, match="strict"):
        formal_gate.verify_strict_cases(tmp_path)
    monkeypatch.setattr(formal_gate, "validate_cases", lambda path: [])
    with pytest.raises(ValueError, match="strict"):
        formal_gate.verify_strict_cases(tmp_path)


def test_formal_gate_cannot_ignore_nested_frozen_file(tmp_path):
    case = blind_zh.load_cases(SAMPLES)[0]
    (tmp_path / "valid.json").write_bytes(jcs_bytes(case))
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "invalid.json").write_bytes(b"{}")
    with pytest.raises(ValueError, match="strict"):
        formal_gate.verify_strict_cases(tmp_path)
