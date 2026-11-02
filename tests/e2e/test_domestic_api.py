"""Exact JSON interfaces expose actual approvals and verified outcomes."""

from threading import Thread

import httpx
import pytest
from typer.testing import CliRunner

from corridor.bench.domestic_runner import run_task
from corridor.cli import app
from corridor.schemas.base import loads_exact


def test_task_default_is_unsigned_but_explicit_events_complete_real_transfer():
    request = {"task": "给Chen转200元", "initial_state": "bank"}
    preview = run_task(request)
    assert preview["final_status"] == "awaiting_input"
    assert preview["dispatch_count"] == 0
    assert preview["approval_events"] == []
    result = run_task({**request, "simulated_device_events": ["confirm"]})
    assert result["final_status"] == "settled"
    assert result["dispatch_count"] == 1
    assert result["approval_events"]
    assert result["audit_valid"]
    assert result["actions"][0]["tier"] == "yellow"


@pytest.mark.parametrize(
    "task,events",
    [
        ({"task": "给Chen转200元"}, ["confirm"]),
        ({"task": "卡丢了，赶紧挂失", "initial_state": "card"}, ["confirm", "mfa", "confirm"]),
    ],
)
def test_extra_device_events_cannot_replace_verified_terminal_outcome(task, events):
    settled = run_task({**task, "simulated_device_events": events})
    repeated = run_task({**task, "simulated_device_events": [*events, "mfa", "confirm"]})
    assert settled["final_status"] == repeated["final_status"] == "settled"
    assert repeated["actions"] == settled["actions"]
    assert repeated["receipts"] == settled["receipts"]
    assert repeated["audit_hash"] == settled["audit_hash"]
    assert repeated["dispatch_count"] == settled["dispatch_count"] == 1


def test_task_does_not_accept_authority_or_unknown_state_fields():
    for request in [
        {"task": "余额", "initial_state": "real_bank"},
        {"task": "余额", "approval_token": "forged"},
    ]:
        with pytest.raises(ValueError):
            run_task(request)


def test_cli_run_task_and_http_return_the_same_structured_outcome(tmp_path):
    from corridor.channels.http import make_server

    task = b'{"task":"balance","initial_state":"bank"}'
    source, output = tmp_path / "task.json", tmp_path / "result.json"
    source.write_bytes(task)
    invocation = CliRunner().invoke(app, ["run-task", "--file", str(source), "--out", str(output)])
    assert invocation.exit_code == 0, invocation.output
    expected = loads_exact(output.read_bytes())
    server = make_server(0)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/run-task"
        response = httpx.post(url, content=task, trust_env=False)
        assert response.status_code == 200
        assert loads_exact(response.content) == expected
        for invalid in [b'{"task":"balance","x":1.1}', b'{"task":"a","task":"b"}']:
            assert httpx.post(url, content=invalid, trust_env=False).status_code == 400
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_chinese_suite_and_intent_corpus_are_complete_and_structurally_checked(tmp_path):
    from corridor.bench.domestic_runner import evaluate_intents, run_zh_suite

    report = run_zh_suite(output=tmp_path)
    assert report["total"] == 33
    assert report["passed"] == report["total"]
    failed = [row for row in report["results"] if not row["passed"]]
    assert failed == []
    partner = run_task({"task": "爱人生辰快到了，花和蛋糕都帮我安排", "task_id": "ZH-BD-03"})
    candidates = partner["responses"][-1]["payload"]["candidates"]
    assert [item["event_ref"] for item in candidates] == ["BIRTHDAY"]
    assert not partner["plans"] and partner["dispatch_count"] == 0
    assert all(row["checks"] for row in report["results"])
    intents = evaluate_intents()
    assert intents["total"] >= 100
    assert intents["intent_correct"] == intents["total"]
    # FIX-D4 measures the deployed session, not its unused model flag. Frozen
    # labels retain these mismatches: no selected card, and refused passwords.
    # G06: INT-ZH-116/121 name a partner the baseline fixture lacks, so they clarify.
    # G11: frozen INT-ZH-014/015 label the old "go to the form" flow; d5 shows the form's
    # own unsigned scheduled card directly, so the frozen clarification label differs.
    assert intents["clarification_correct"] == 131
    mismatches = {row["id"]: row for row in intents["results"] if not row["clarification_correct"]}
    partner_rows = {"INT-ZH-116", "INT-ZH-121"}
    scheduled_rows = {"INT-ZH-014", "INT-ZH-015"}
    assert set(mismatches) == {
        *(f"INT-ZH-{index:03d}" for index in range(72, 86)),
        *(f"INT-ZH-{index:03d}" for index in range(126, 132)),
        *partner_rows,
        *scheduled_rows,
    }
    texts = {"INT-ZH-014": "下周五给Chen转3000元", "INT-ZH-015": "明天给Chen转50元"}
    for identifier, row in mismatches.items():
        if identifier in partner_rows:
            assert row["actual"]["needs_clarification"] is True
            assert row["session_status"] == "clarify"
            continue
        if identifier in scheduled_rows:
            assert row["actual"]["needs_clarification"] is False
            assert row["session_status"] == "awaiting_approval"
            assert row["parameters"] == row["expected_parameters"]
            scheduled = run_task({"task": texts[identifier], "task_id": identifier})
            assert scheduled["actions"][0]["card"]["kind"] == "schedule_transfer"
            assert scheduled["dispatch_count"] == 0 and not scheduled["approval_events"]
            continue
        card_selection = int(identifier.rsplit("-", 1)[1]) < 100
        assert row["actual"]["needs_clarification"] is card_selection
        assert row["session_status"] == (
            "awaiting_device_selection" if card_selection else "refused"
        )
