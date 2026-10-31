"""Offline regression coverage for the read-only PowerShell status probe."""

import subprocess
from copy import deepcopy

import pytest

from corridor.crypto.jcs import jcs_bytes
from scripts import run_live_zh as runner

STATUS = {
    "managed_process_running": True,
    "loopback_only_verified": True,
    "api_error": None,
    "version": {"version": "0.40.5"},
    "available_models": {"models": [{"name": "synthetic-model", "digest": "synthetic-digest"}]},
}
STATUS_ERROR = "status.ps1 无法执行或返回不完整"


def mock_status(monkeypatch, payload, *, stderr=b"", returncode=0):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, returncode, payload, stderr)

    monkeypatch.setattr(runner.subprocess, "run", run)
    return calls


def test_status_uses_process_execution_policy_and_accepts_nullable_api_error(monkeypatch):
    calls = mock_status(monkeypatch, jcs_bytes(STATUS))
    assert runner.local_status() == STATUS
    command, kwargs = calls[0]
    assert command[:6] == [
        "powershell",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        command[-1],
    ]
    assert "status.ps1" in command[-1]
    assert "Set-ExecutionPolicy" not in command[-1]
    assert kwargs["capture_output"] is True
    assert kwargs["timeout"] == 30


@pytest.mark.parametrize(
    "path",
    [
        ("managed_process_running",),
        ("loopback_only_verified",),
        ("version",),
        ("version", "version"),
        ("available_models",),
        ("available_models", "models"),
        ("available_models", "models", 0, "name"),
        ("available_models", "models", 0, "digest"),
    ],
)
@pytest.mark.parametrize("remove", [False, True], ids=["null", "missing"])
def test_status_rejects_each_incomplete_required_field(monkeypatch, path, remove):
    status = deepcopy(STATUS)
    parent = status
    for part in path[:-1]:
        parent = parent[part]
    if remove:
        del parent[path[-1]]
    else:
        parent[path[-1]] = None
    mock_status(monkeypatch, jcs_bytes(status), stderr=b"Script execution is disabled\n")
    with pytest.raises(ValueError, match=STATUS_ERROR) as raised:
        runner.local_status()
    assert "Script execution is disabled" in str(raised.value)
    assert "ownership" not in str(raised.value)


@pytest.mark.parametrize(
    "status",
    [
        None,
        [],
        {key: value for key, value in STATUS.items() if key != "api_error"},
        {**STATUS, "managed_process_running": "true"},
        {**STATUS, "version": {"version": ""}},
        {**STATUS, "available_models": {"models": [None]}},
        {**STATUS, "available_models": {"models": {}}},
        {**STATUS, "available_models": {"models": [{"name": "", "digest": ""}]}},
    ],
)
def test_status_rejects_invalid_shapes_and_empty_projected_identities(monkeypatch, status):
    mock_status(monkeypatch, jcs_bytes(status))
    with pytest.raises(ValueError, match=STATUS_ERROR):
        runner.local_status()


@pytest.mark.parametrize("field", ["managed_process_running", "loopback_only_verified"])
def test_status_preserves_real_ownership_failures(monkeypatch, field):
    status = {**STATUS, field: False}
    mock_status(monkeypatch, jcs_bytes(status))
    result = runner.local_status()
    assert result == status
    with pytest.raises(ValueError, match="ownership/loopback"):
        runner.verify_local_lock([], result)


def test_status_accepts_empty_model_list_for_separate_lock_verification(monkeypatch):
    status = {**STATUS, "available_models": {"models": []}}
    mock_status(monkeypatch, jcs_bytes(status))
    assert runner.local_status() == status


@pytest.mark.parametrize(
    ("payload", "returncode"),
    [(b"", 0), (b"not JSON", 0), (jcs_bytes(STATUS), 1)],
)
def test_status_reports_process_and_parse_failures(monkeypatch, payload, returncode):
    mock_status(monkeypatch, payload, stderr=b"PSSecurityException\n", returncode=returncode)
    with pytest.raises(ValueError, match=STATUS_ERROR) as raised:
        runner.local_status()
    assert "PSSecurityException" in str(raised.value)
    assert raised.value.__suppress_context__


@pytest.mark.parametrize("failure", ["timeout", "missing_executable"])
def test_status_reports_launch_failures_without_raw_exception_details(monkeypatch, failure):
    def fail(*args, **kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired("powershell", 30, stderr=b"status timed out\n")
        raise FileNotFoundError("synthetic-private-exception-detail")

    monkeypatch.setattr(runner.subprocess, "run", fail)
    with pytest.raises(ValueError, match=STATUS_ERROR) as raised:
        runner.local_status()
    assert "synthetic-private-exception-detail" not in str(raised.value)
    if failure == "timeout":
        assert "status timed out" in str(raised.value)


def test_status_stderr_is_bounded_and_does_not_disclose_keys(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "synthetic-inherited-credential")
    stderr = (
        "Script execution is disabled\n"
        "OPENAI_API_KEY=synthetic-file-credential\n"
        "Authorization: Bearer synthetic-bearer-credential\n"
        "sk-synthetic-unlabelled-credential\n"
        "diagnostic: synthetic-inherited-credential " + "x" * 2000 + "\n"
        "sixth line must be omitted\n"
    ).encode("utf-8")
    mock_status(monkeypatch, jcs_bytes({}), stderr=stderr)
    with pytest.raises(ValueError, match=STATUS_ERROR) as raised:
        runner.local_status()
    message = str(raised.value)
    assert "Script execution is disabled" in message
    assert "sixth line" not in message
    assert len(message) < 1800
    for secret in (
        "synthetic-file-credential",
        "synthetic-bearer-credential",
        "sk-synthetic-unlabelled-credential",
        "synthetic-inherited-credential",
    ):
        assert secret not in message


@pytest.mark.parametrize(
    "stderr",
    [
        '{"access_token": "opaque-synthetic-value"}',
        "{'token': 'opaque-synthetic-value'}",
        "API_KEY=\nopaque-synthetic-value\ncontinued-synthetic-value",
    ],
)
def test_status_redacts_quoted_credentials_and_continuation_lines(monkeypatch, stderr):
    mock_status(monkeypatch, b"", stderr=("Policy error\n" + stderr).encode("utf-8"))
    with pytest.raises(ValueError, match=STATUS_ERROR) as raised:
        runner.local_status()
    assert "Policy error" in str(raised.value)
    assert "opaque-synthetic-value" not in str(raised.value)
    assert "continued-synthetic-value" not in str(raised.value)
