"""S7 external review 3: one regression per must item not covered by the second round.

1  the access code never enters the Streamlit session: the access field hashes it in the
   browser with a single-use challenge; the configuration keeps no code;
2  no key or code is reachable from the session object graph between calls, nor in page
   text, exception text, the journal or the ledger (sentinel scan over success, 401,
   timeout, format failure and record failure);
3  locking after a grant ends the grant, and every call checks it;
4  a grant is bound to the credential version: a rotated code or key revokes it at once;
5  the reservation is a proven bound: the frozen client refuses, before sending, any
   request whose own bound exceeds what the ledger reserved; the longest accepted request
   fits;
6  a request that was sent but has no usage settles at its pre-send bound, never at zero;
7  concurrent calls on one client are serialized, each settled with its own trace;
8  switching the process off stops new external calls at once, also mid-transaction;
9  with the switch off nothing but the switch is read;
10 a settlement the ledger refuses keeps the reservation, never replaces the call's own
   result or error, and is shown as such;
11 a ledger fault is not "cap reached", and live access recovers with the ledger;
12 a client that cannot be rebuilt at the boundary returns the page to the scripted demo
   and says so;
13 the currency contract: a CNY deployment's cost is in CNY end to end;
14 absolute promises and unverified price claims are gone from the copy.

No test calls a model: transports are replaced in-process and sockets are refused.
"""

import hashlib
import hmac
import json
import os
import secrets
import shutil
import sqlite3
import subprocess
import threading
import traceback
import types
from collections.abc import Mapping
from fractions import Fraction
from pathlib import Path

import httpx
import pytest

from corridor.agent.llm import BudgetExceeded, LLMOutputError, ReplayCorrupt
from corridor.ui import live_config, live_controls, live_runtime
from corridor.ui.live_config import DEPLOYMENTS, LiveConfig
from corridor.ui.live_runtime import JournalClient, LiveSession, access_proof
from corridor.ui.messages import UI_MESSAGES
from tests.unit.test_ui_live_mode import (
    ACCESS_FIELD,
    FakeModel,
    Owner,
    _access_field,  # noqa: F401 - the autouse fixture that records the access field
    _no_inherited_client_settings,  # noqa: F401 - autouse: no inherited LLM_* settings
    badge,
    call,
    environ,
    send,
    shown_text,
    start,
    switch,
    unlock,
)
from tests.unit.test_ui_live_review import (
    GENERIC,
    confirm,
    failure_line,
    msg,
    no_network,
    response,
)

UI = Path(live_controls.__file__).parent
SCHEMA = {
    "type": "object",
    "properties": {"intent": {"type": "string"}},
    "required": ["intent"],
    "additionalProperties": False,
}


def sentinel_dir(tmp_path, key, code, files=(".env",)):
    directory = tmp_path / "sentinel-creds"
    directory.mkdir(parents=True, exist_ok=True)
    for name in files:
        (directory / name).write_text(
            f"LLM_API_KEY={key}\nLIVE_ACCESS_CODE={code}\n", encoding="utf-8"
        )
    return directory


def schema_call(client, step="classify", text="balance"):
    return client.complete(
        task_id="S7X",
        step=step,
        prompt={"instruction": "classify", "user_text": text, "response_schema": SCHEMA},
        tool_state={},
        scripted=lambda: {"intent": "balance_query"},
    )


def provider_response(request, *, chat, output, prompt_tokens=120, completion_tokens=12):
    """A well-formed provider answer: one forced emit_result call and valid usage."""
    payload = json.loads(request.content)
    if chat:
        body = {
            "id": "resp-test",
            "model": payload["model"],
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "type": "function",
                                "function": {
                                    "name": "emit_result",
                                    "arguments": json.dumps(output),
                                },
                            }
                        ],
                    },
                }
            ],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        }
    else:
        body = {
            "id": "resp-test",
            "model": payload["model"],
            "status": "completed",
            "output": [
                {
                    "type": "function_call",
                    "name": "emit_result",
                    "status": "completed",
                    "call_id": "call-1",
                    "arguments": json.dumps(output),
                }
            ],
            "usage": {
                "input_tokens": prompt_tokens,
                "output_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
                "output_tokens_details": {"reasoning_tokens": 0},
            },
        }
    return httpx.Response(200, json=body, request=request)


class Transport:
    """Stands in for httpx's transport: counts requests and answers per behaviour."""

    def __init__(self, monkeypatch, behaviour="ok", *, output=None, usage=(120, 12)):
        self.behaviour, self.requests = behaviour, []
        self.output = output or {"intent": "balance_query"}
        self.usage = usage
        transport = self

        def handle(self_, request):
            transport.requests.append(request)
            chat = request.url.path.endswith("/chat/completions")
            if transport.behaviour == "timeout":
                raise httpx.ReadTimeout("test: simulated provider timeout", request=request)
            if transport.behaviour == "401":
                echo = request.headers.get("authorization", "")
                return httpx.Response(
                    401, json={"error": {"message": "bad key " + echo}}, request=request
                )
            return provider_response(
                request,
                chat=chat,
                output=transport.output,
                prompt_tokens=transport.usage[0],
                completion_tokens=transport.usage[1],
            )

        monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle)


def ledger_rows(root):
    connection = sqlite3.connect(Path(root) / live_runtime.SpendLedger.FILE)
    try:
        return connection.execute(
            "SELECT currency, deployment, amount, settled FROM spend ORDER BY id"
        ).fetchall()
    finally:
        connection.close()


# --- a recursive scan of everything an object can reach --------------------------------------

_LEAVES = (int, float, bool, complex, type(None), Fraction)
_OPAQUE = (
    types.ModuleType,
    type,
    types.FunctionType,
    types.BuiltinFunctionType,
    types.MethodType,
    types.BuiltinMethodType,
    types.CodeType,
    types.FrameType,
)


def reachable_text(root, limit=400_000):
    """Every string and byte string reachable from ``root`` through containers, instance
    dictionaries and slots (functions, classes and modules are not followed)."""
    seen, stack, texts = set(), [root], []
    while stack and len(seen) < limit:
        item = stack.pop()
        if id(item) in seen:
            continue
        seen.add(id(item))
        if isinstance(item, str):
            texts.append(item)
        elif isinstance(item, (bytes, bytearray)):
            texts.append(bytes(item).decode("utf-8", "replace"))
        elif isinstance(item, _LEAVES) or isinstance(item, _OPAQUE):
            continue
        elif isinstance(item, Mapping):
            stack.extend(item.keys())
            stack.extend(item.values())
        elif isinstance(item, (list, tuple, set, frozenset)):
            stack.extend(item)
        elif isinstance(item, Path):
            texts.append(str(item))
        else:
            if hasattr(item, "SerializeToString"):
                try:
                    texts.append(item.SerializeToString().decode("utf-8", "replace"))
                except Exception:  # a protobuf that cannot serialize holds nothing to scan
                    pass
            namespace = getattr(item, "__dict__", None)
            if isinstance(namespace, dict):
                stack.append(namespace)
            for cls in type(item).__mro__:
                for slot in getattr(cls, "__slots__", ()) or ():
                    if isinstance(slot, str) and hasattr(item, slot):
                        stack.append(getattr(item, slot))
    return texts


def exception_text(error):
    parts = traceback.format_exception(error)
    link = error
    for _ in range(8):
        link = link.__cause__ or link.__context__
        if link is None:
            break
        parts.extend(traceback.format_exception(link))
    return "".join(parts)


# --- 1: the access code never enters the session ------------------------------------------


def test_the_access_code_never_enters_the_session_and_proofs_are_single_use(monkeypatch, tmp_path):
    code = "CODE-" + secrets.token_hex(12)
    monkeypatch.setenv(live_config.ACCESS_CODE, code)
    page = start(monkeypatch, tmp_path)
    live = page.session_state["live_session"]
    # The configuration keeps no code; the field is drawn with the current challenge.
    assert not hasattr(live.config, "access_code") and code not in repr(live.config)
    assert ACCESS_FIELD and ACCESS_FIELD[-1]["nonce"] == live.challenge()
    assert set(ACCESS_FIELD[-1]) == {"nonce", "label", "button", "disabled", "key"}
    assert not [item for item in page.text_input if item.key == "live_access_code"]
    nonce = live.challenge()
    proof = access_proof(code, nonce)
    unlock(page, code)
    assert live.snapshot()["authorized"] is True
    # The challenge is used up: the same proof again is stale, not a failure.
    assert live.challenge() != nonce
    assert live.authorize_proof(nonce, proof) == "stale"
    assert live.snapshot()["locked"] is False
    # Nothing reachable from the session holds the code.
    assert not [text for text in reachable_text(page.session_state._state) if code in text]
    assert code not in shown_text(page)


def test_the_browser_field_sends_only_a_nonce_bound_hmac(tmp_path):
    html = (UI / "live_access" / "index.html").read_text(encoding="utf-8")
    script = (UI / "live_access" / "hmac.js").read_text(encoding="utf-8")
    # One value is ever sent: the nonce and the proof; the field is cleared first.
    assert html.count("streamlit:setComponentValue") == 1
    assert "value: { nonce: nonce, proof: proof }" in html
    assert 'field.value = "";' in html and 'type="password"' in html
    assert "fetch(" not in html + script and "XMLHttpRequest" not in html + script
    assert '"corridor-live-access-v1|"' in script
    assert live_runtime.PROOF_CONTEXT == "corridor-live-access-v1|"
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed: the HMAC cross-check needs a JavaScript runtime")
    cases = [
        ["", ""],
        ["judge-pass-2026-Kx7Qm9Rv", "0123abcd"],
        ["评委访问码🔑", "f" * 32],
        ["k" * 100, "x" * 200],
        ["a" * 64, "b" * 55],
        ["a" * 65, "b" * 56],
    ]
    runner = tmp_path / "check.js"
    runner.write_text(
        "const api = require(process.argv[2]);\n"
        "const cases = JSON.parse(process.argv[3]);\n"
        "console.log(JSON.stringify(cases.map(([code, nonce]) => api.proof(code, nonce))));\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [node, str(runner), str(UI / "live_access" / "hmac.js"), json.dumps(cases)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    assert json.loads(result.stdout) == [access_proof(code, nonce) for code, nonce in cases]
    assert (
        access_proof("k", "n")
        == hmac.new(b"k", b"corridor-live-access-v1|n", hashlib.sha256).hexdigest()
    )


def test_with_the_switch_off_nothing_but_the_switch_is_read():
    class OnlySwitch(dict):
        def get(self, name, default=None):
            assert name == live_config.SWITCH, name
            return super().get(name, default)

        def __getitem__(self, name):
            raise AssertionError(name)

    assert LiveConfig.from_environ(OnlySwitch({live_config.SWITCH: "0"})) == LiveConfig()
    assert LiveConfig.from_environ(OnlySwitch()) == LiveConfig()
    assert "access_code" not in {field for field in LiveConfig.__dataclass_fields__}


# --- 2: no secret reachable from the session, the page, errors, the journal or the ledger -----


def test_no_sentinel_secret_is_reachable_after_every_kind_of_outcome(monkeypatch, tmp_path):
    key, code, wrong = (
        "sk-SENTINEL-" + secrets.token_hex(16),
        "CODE-SENTINEL-" + secrets.token_hex(8),
        "WRONG-SENTINEL-" + secrets.token_hex(8),
    )
    creds = sentinel_dir(tmp_path, key, code)
    secrets_ = (key, code, wrong)

    def leaks(texts):
        return [text for text in texts for secret in secrets_ if secret in text]

    def file_leaks(root):
        found = []
        for path in Path(root).rglob("*"):
            if path.is_file():
                data = path.read_bytes()
                found.extend(path for secret in secrets_ if secret.encode("utf-8") in data)
        return found

    with monkeypatch.context() as patch:
        fake = FakeModel(patch)
        page = start(patch, tmp_path / "page", **{live_config.ENV_DIR: str(creds)})
        unlock(page, wrong)
        unlock(page, code)
        switch(page, True)
        controller = page.session_state["controller"]
        send(page, "给妈妈转800元")
        confirm(page)
        assert response(page)["status"] == "settled"
        for behaviour in ("bad", "error", "timeout"):
            fake.behaviour = behaviour
            send(page, "余额多少")
        assert isinstance(controller.client, JournalClient)
        assert controller.client._api_key == ""
        assert not leaks(reachable_text(page.session_state._state))
        assert not leaks([shown_text(page)])
        assert not leaks([str(item.value) for item in page.exception])
        assert not file_leaks(tmp_path / "page" / "journal")
    # The frozen transport path too: a 401 whose body echoes the request's Authorization
    # header, a timeout, and a journal that cannot write the record.
    values = environ(tmp_path / "unit", **{live_config.ENV_DIR: str(creds)})
    with no_network(monkeypatch):
        for behaviour in ("401", "timeout", "record"):
            with monkeypatch.context() as patch:
                transport = Transport(patch, "ok" if behaviour == "record" else behaviour)
                session = LiveSession(LiveConfig.from_environ(values), environ=values)
                assert session.authorize(code) == "accepted"
                client = session.client(Owner(), context_guard=None)
                if behaviour == "record":
                    original = Path.open

                    def refuse_record(self, mode="r", *args, original=original, **kwargs):
                        if mode == "xb":
                            raise OSError("test: the journal disk is full")
                        return original(self, mode, *args, **kwargs)

                    patch.setattr(Path, "open", refuse_record)
                with pytest.raises((LLMOutputError, OSError, ReplayCorrupt)) as raised:
                    schema_call(client)
            assert len(transport.requests) == 1, behaviour
            assert client._api_key == ""
            assert not leaks([exception_text(raised.value)]), behaviour
            assert raised.value.__context__ is None or behaviour == "record"
            assert not leaks(reachable_text(session)), behaviour
            assert not leaks([json.dumps(session.snapshot(), default=str)])
    assert not file_leaks(tmp_path / "unit" / "journal")


# --- 3 and 4: locks and credential versions end a grant at once -------------------------------


def test_locking_after_a_grant_ends_it_and_every_call_checks(monkeypatch, tmp_path):
    fake = FakeModel(monkeypatch)
    values = environ(tmp_path)
    session = LiveSession(LiveConfig.from_environ(values), environ=values)
    assert session.authorize("judge-pass-2026-Kx7Qm9Rv") == "accepted"
    client = session.client(Owner(), context_guard=None)
    assert call(client, "one").source == "live-recorded"
    for _ in range(live_runtime.LOCK_AFTER_FAILURES):
        assert session.authorize("wrong") == "invalid"
    info = session.snapshot()
    assert info["reason"] == "locked" and info["available"] is False
    assert info["authorized"] is False
    with pytest.raises(BudgetExceeded):
        call(client, "two")
    assert fake.attempts == 1
    assert session.last_call() == {"source": "live-revoked", "failed": True}
    # The correct code no longer unlocks a locked session.
    assert session.authorize("judge-pass-2026-Kx7Qm9Rv") == "locked"


@pytest.mark.parametrize("change", ["file_code", "file_key", "env_code"])
def test_a_rotated_code_or_key_revokes_the_grant_before_the_next_call(
    monkeypatch, tmp_path, change
):
    fake = FakeModel(monkeypatch)
    values = environ(tmp_path)
    if change == "env_code":
        values[live_config.ACCESS_CODE] = "env-code-1-Kx7Qm9Rv2Lp"
    session = LiveSession(LiveConfig.from_environ(values), environ=values)
    first = "env-code-1-Kx7Qm9Rv2Lp" if change == "env_code" else "judge-pass-2026-Kx7Qm9Rv"
    assert session.authorize(first) == "accepted"
    client = session.client(Owner(), context_guard=None)
    assert call(client, "one").source == "live-recorded"
    path = Path(values[live_config.ENV_DIR]) / ".env"
    text = path.read_text(encoding="utf-8")
    if change == "file_code":
        path.write_text(
            text.replace("judge-pass-2026-Kx7Qm9Rv", "judge-pass-2027-Kx7Qm9Rv"), encoding="utf-8"
        )
        second = "judge-pass-2027-Kx7Qm9Rv"
    elif change == "file_key":
        path.write_text(text.replace("sk-test-only", "sk-rotated"), encoding="utf-8")
        second = "judge-pass-2026-Kx7Qm9Rv"
    else:
        values[live_config.ACCESS_CODE] = "env-code-2-Kx7Qm9Rv2Lp"
        second = "env-code-2-Kx7Qm9Rv2Lp"
    # No boundary needed: the very next call is refused before any transport attempt.
    with pytest.raises(BudgetExceeded):
        call(client, "two")
    assert fake.attempts == 1
    assert session.last_call() == {"source": "live-revoked", "failed": True}
    info = session.snapshot()
    assert info["reason"] == "access_required" and info["revoked"] == "credentials"
    # The old code no longer unlocks after a code change; the current one does.
    if change != "file_key":
        assert session.authorize(first) == "invalid"
    assert session.authorize(second) == "accepted"
    assert call(client, "three").source == "live-recorded"
    assert fake.attempts == 2


# --- 5 and 6: the reservation is a proven bound; sent requests never settle at zero -----------


def test_the_frozen_client_refuses_a_request_whose_bound_exceeds_the_reservation(
    monkeypatch, tmp_path
):
    values = environ(tmp_path)
    with no_network(monkeypatch):
        transport = Transport(monkeypatch)
        session = LiveSession(LiveConfig.from_environ(values), environ=values)
        assert session.authorize("judge-pass-2026-Kx7Qm9Rv") == "accepted"
        client = session.client(Owner(), context_guard=None)
        # The longest request the page accepts (4000 characters) fits the reservation.
        result = schema_call(client, "long", "转" * 3990 + "给妈妈转800元")
        assert result.source == "live-recorded" and len(transport.requests) == 1
        body = len(transport.requests[0].content)
        assert body + 1024 <= live_runtime.RESERVE_INPUT_TOKENS
        rows = ledger_rows(tmp_path / "journal")
        assert rows[-1][3] == 1 and Fraction(rows[-1][2]) == client.calls[-1]["cost_usd"]
        # With a reservation smaller than the request's own bound, nothing is sent.
        monkeypatch.setattr(live_runtime, "RESERVE_INPUT_TOKENS", 512)
        with pytest.raises(BudgetExceeded):
            schema_call(client, "too-long", "给妈妈转800元" * 40)
        assert len(transport.requests) == 1
        assert client.calls[-1]["source"] == "budget-rejected"
        assert session.outcomes()[-1]["settlement"] == "released"
        assert Fraction(ledger_rows(tmp_path / "journal")[-1][2]) == 0


@pytest.mark.parametrize("behaviour", ["401", "timeout"])
def test_a_sent_request_without_usage_settles_at_its_bound_never_zero(
    monkeypatch, tmp_path, behaviour
):
    values = environ(tmp_path)
    with no_network(monkeypatch):
        transport = Transport(monkeypatch, behaviour)
        session = LiveSession(LiveConfig.from_environ(values), environ=values)
        assert session.authorize("judge-pass-2026-Kx7Qm9Rv") == "accepted"
        client = session.client(Owner(), context_guard=None)
        with pytest.raises(LLMOutputError):
            schema_call(client)
    assert len(transport.requests) == 1
    trace = client.calls[-1]
    assert trace["source"] == "live-error" and "input_tokens" not in trace
    bound = trace["cost_usd"]
    reservation = LiveSession._reservation(DEPLOYMENTS[0])
    assert 0 < bound <= reservation
    rows = ledger_rows(tmp_path / "journal")
    assert rows == [("USD", DEPLOYMENTS[0].id, live_runtime.fraction_string(bound), 1)]
    assert session.outcomes()[-1]["settlement"] == "bound"
    assert Fraction(session.snapshot()["spent"]["USD"]) == bound


# --- 7: one client, one call at a time, one trace each --------------------------------------


def test_concurrent_calls_on_one_client_are_serialized_and_settled_separately(
    monkeypatch, tmp_path
):
    session = LiveSession(LiveConfig.from_environ(environ(tmp_path)))
    assert session.authorize("judge-pass-2026-Kx7Qm9Rv") == "accepted"
    client = session.client(Owner(), context_guard=None)
    costs = {"first": Fraction(3, 100), "second": Fraction(7, 100)}
    inside_first, release_first, second_inside = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    overlap = []

    def live(client_, prompt, state, trace):
        step = trace["step"]
        if step == "first":
            inside_first.set()
            release_first.wait(10)
        else:
            second_inside.set()
            overlap.append(not release_first.is_set())
        client_.api_calls += 1
        client_.spent_usd += costs[step]
        trace.update(
            input_tokens=100,
            output_tokens=10,
            reasoning_tokens=0,
            total_tokens=110,
            cost_usd=costs[step],
        )
        return client_._output({"intent": "balance_query", "needs_clarification": False})

    monkeypatch.setattr(JournalClient, "_live", live)
    errors = []

    def run(step):
        try:
            call(client, step)
        except Exception as error:  # surfaced below
            errors.append(error)

    first = threading.Thread(target=run, args=("first",))
    second = threading.Thread(target=run, args=("second",))
    first.start()
    assert inside_first.wait(10)
    second.start()
    # The second call cannot start while the first holds the client.
    assert not second_inside.wait(0.5)
    release_first.set()
    first.join(10)
    second.join(10)
    assert not errors and overlap == [False]
    amounts = sorted(Fraction(row[2]) for row in ledger_rows(tmp_path / "journal"))
    assert amounts == [Fraction(3, 100), Fraction(7, 100)]
    assert [item["settlement"] for item in session.outcomes()] == ["settled", "settled"]
    assert Fraction(session.snapshot()["spent"]["USD"]) == Fraction(10, 100)


# --- 8: switching off stops new external calls at once ---------------------------------------


def test_switching_the_process_off_stops_new_calls_at_once_even_mid_transaction(
    monkeypatch, tmp_path
):
    fake = FakeModel(monkeypatch)
    page = start(monkeypatch, tmp_path)
    unlock(page)
    switch(page, True)
    controller = page.session_state["controller"]
    send(page, "给妈妈转800元")
    attempts = fake.attempts
    assert controller.at_transaction_boundary() is False
    monkeypatch.setenv(live_config.SWITCH, "0")
    # The confirm callback runs before the page draws anything: the client itself
    # refuses, so not one more transport attempt happens.
    confirm(page)
    assert fake.attempts == attempts
    assert controller.gateway.bank.execute_count == 0
    text = shown_text(page)
    assert failure_line("live-revoked") in text
    assert msg("live.reason.revoked_pending") in text
    assert msg("live.badge.live_revoked", deployment=DEPLOYMENTS[0].label) in badge(page)[-1]
    assert msg("live.judge_strip_revoked", deployment=DEPLOYMENTS[0].label) in text
    assert msg("live.off_hint") not in text
    # Withdrawing the action reaches the boundary: the scripted planner takes over.
    page.button(key="domestic_cancel").click().run()
    assert not page.exception
    assert controller.client.mode == "scripted"
    assert msg("live.badge.scripted") in badge(page)[-1]
    assert msg("live.off_hint") in shown_text(page)
    send(page, "余额多少")
    assert fake.attempts == attempts


def test_an_invalid_configuration_mid_transaction_is_named_honestly(monkeypatch, tmp_path):
    fake = FakeModel(monkeypatch)
    page = start(monkeypatch, tmp_path)
    unlock(page)
    switch(page, True)
    send(page, "给妈妈转800元")
    attempts = fake.attempts
    monkeypatch.setenv(live_config.SESSION_CALLS, "two")
    page.run()
    text = shown_text(page)
    assert msg("live.config_invalid_live") in text and msg("live.config_invalid") not in text
    confirm(page)
    assert fake.attempts == attempts and failure_line("live-revoked") in shown_text(page)


# --- 10 and 11: settlement failures and ledger faults are told apart -------------------------


def test_a_failed_settlement_keeps_the_reservation_and_the_calls_own_outcome(monkeypatch, tmp_path):
    fake = FakeModel(monkeypatch)
    session = LiveSession(LiveConfig.from_environ(environ(tmp_path)))
    assert session.authorize("judge-pass-2026-Kx7Qm9Rv") == "accepted"
    client = session.client(Owner(), context_guard=None)
    original = live_runtime.SpendLedger.reserve

    def reserve(self, *args, **kwargs):
        original(self, *args, **kwargs)

        def settle(actual):
            raise live_runtime.LedgerUnavailable("test: settlement refused")

        return settle

    monkeypatch.setattr(live_runtime.SpendLedger, "reserve", reserve)
    reservation = LiveSession._reservation(DEPLOYMENTS[0])
    # A successful call still returns its result; its settlement is recorded as failed.
    assert call(client, "one").source == "live-recorded"
    assert session.outcomes()[-1]["settlement"] == "failed"
    rows = ledger_rows(tmp_path / "journal")
    assert rows == [("USD", DEPLOYMENTS[0].id, live_runtime.fraction_string(reservation), 0)]
    # A failing call keeps its own error, never the bookkeeping one.
    fake.behaviour = "bad"
    with pytest.raises(LLMOutputError):
        call(client, "two")
    assert session.outcomes()[-1]["settlement"] == "failed"
    assert session.outcomes()[-1]["source"] == "output-rejected"
    assert all(row[3] == 0 for row in ledger_rows(tmp_path / "journal"))


def test_the_page_names_a_failed_settlement_to_judges_only(monkeypatch, tmp_path):
    FakeModel(monkeypatch)
    original = live_runtime.SpendLedger.reserve

    def reserve(self, *args, **kwargs):
        original(self, *args, **kwargs)

        def settle(actual):
            raise live_runtime.LedgerUnavailable("test: settlement refused")

        return settle

    monkeypatch.setattr(live_runtime.SpendLedger, "reserve", reserve)
    page = start(monkeypatch, tmp_path)
    unlock(page)
    switch(page, True)
    send(page, "余额多少")
    assert msg("live.note.settlement_failed") in shown_text(page)
    customer = start(monkeypatch, tmp_path / "customer", view="customer")
    unlock(customer)
    switch(customer, True)
    send(customer, "余额多少")
    assert msg("live.note.settlement_failed") not in shown_text(customer)


def test_a_ledger_fault_is_not_a_spent_cap_and_recovers_with_the_ledger(monkeypatch, tmp_path):
    fake = FakeModel(monkeypatch)
    session = LiveSession(LiveConfig.from_environ(environ(tmp_path)))
    assert session.authorize("judge-pass-2026-Kx7Qm9Rv") == "accepted"
    client = session.client(Owner(), context_guard=None)
    original = live_runtime.SpendLedger._connection

    def broken(self):
        raise sqlite3.OperationalError("test: database is locked")

    monkeypatch.setattr(live_runtime.SpendLedger, "_connection", broken)
    info = session.snapshot()
    assert info["reason"] == "ledger_unavailable" and info["ledger_fault"] is True
    with pytest.raises(live_runtime.LedgerUnavailable):
        call(client, "one")
    assert fake.attempts == 0
    assert session.last_call() == {"source": "ledger-unavailable", "failed": True}
    monkeypatch.setattr(live_runtime.SpendLedger, "_connection", original)
    assert session.snapshot()["reason"] == "ready"
    assert call(client, "two").source == "live-recorded"
    assert (
        UI_MESSAGES["live.reason.ledger_unavailable"]["zh-CN"]
        != UI_MESSAGES["live.reason.budget_exhausted"]["zh-CN"]
    )
    # A spent cap is still "budget_exhausted".
    tiny = LiveSession(
        LiveConfig.from_environ(environ(tmp_path / "tiny", **{live_config.BUDGET_USD: "0"}))
    )
    assert tiny.authorize("judge-pass-2026-Kx7Qm9Rv") == "accepted"
    assert tiny.snapshot()["reason"] == "budget_exhausted"


def test_a_symbolic_link_is_refused_for_the_ledger(tmp_path):
    target = tmp_path / "elsewhere"
    target.mkdir()
    link = tmp_path / "journal"
    try:
        os.symlink(target, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this platform does not let the test create a symbolic link")
    session = LiveSession(LiveConfig.from_environ(environ(tmp_path)))
    assert session.snapshot()["reason"] in {"ledger_unavailable", "access_required"}
    assert session.authorize("judge-pass-2026-Kx7Qm9Rv") == "unavailable"
    assert not (target / live_runtime.SpendLedger.FILE).exists()


# --- 12: a client that cannot be rebuilt at the boundary --------------------------------------


def test_a_client_that_cannot_be_rebuilt_returns_to_scripted_and_says_so(monkeypatch, tmp_path):
    FakeModel(monkeypatch)
    page = start(monkeypatch, tmp_path)
    unlock(page)
    switch(page, True)
    controller = page.session_state["controller"]
    assert isinstance(controller.client, JournalClient)

    def unwritable(self, item):
        raise OSError("test: journal directory not writable")

    monkeypatch.setattr(LiveSession, "_journal_dir", unwritable)
    # A changed cap is a new configuration: the old client is dropped, a new one fails.
    monkeypatch.setenv(live_config.SESSION_CALLS, "39")
    page.run()
    assert controller.client.mode == "scripted"
    text = shown_text(page)
    assert msg("live.client_failed_returned") in text and msg("live.client_failed") not in text
    assert msg("live.badge.scripted") in badge(page)[-1]


# --- 13: the currency contract ------------------------------------------------------------------


def test_a_cny_deployment_costs_cny_end_to_end_and_never_touches_usd(monkeypatch, tmp_path):
    files = sorted({item.env_file for item in DEPLOYMENTS})
    values = environ(tmp_path)
    for name in files:
        (Path(values[live_config.ENV_DIR]) / name).write_text(
            "LLM_API_KEY=sk-fake\nLIVE_ACCESS_CODE=judge-pass-2026-Kx7Qm9Rv\n", encoding="utf-8"
        )
    item = live_config.deployment("deepseek-flash")
    with no_network(monkeypatch):
        Transport(monkeypatch, usage=(1000, 100))
        session = LiveSession(LiveConfig.from_environ(values), environ=values)
        session.select(item.id)
        assert session.authorize("judge-pass-2026-Kx7Qm9Rv") == "accepted"
        client = session.client(Owner(), context_guard=None)
        assert (client.assumed_price_in, client.assumed_price_out) == (Fraction(2), Fraction(8))
        schema_call(client)
    expected = Fraction(1000 * 2 + 100 * 8, 1_000_000)
    assert client.calls[-1]["cost_usd"] == expected
    assert ledger_rows(tmp_path / "journal") == [
        ("CNY", item.id, live_runtime.fraction_string(expected), 1)
    ]
    info = session.snapshot()
    assert Fraction(info["spent"]["CNY"]) == expected and Fraction(info["spent"]["USD"]) == 0
    manifest = json.loads((client.replay_dir / live_runtime.MANIFEST).read_text(encoding="utf-8"))
    assert manifest["currency"] == "CNY" and "in CNY" in manifest["cost_field_note"]
    # The reservation is priced in the deployment's own currency.
    assert Fraction(info["reservation"]) == Fraction(
        live_runtime.RESERVE_INPUT_TOKENS * 2 + 512 * 8, 1_000_000
    )


# --- 14: the copy ------------------------------------------------------------------------------


def test_absolute_promises_and_unverified_price_claims_are_gone():
    def zh(key):
        return UI_MESSAGES[key]["zh-CN"]

    assert "完全一样" not in zh("live.intro")
    assert "不调用任何 API" not in zh("live.off_hint")
    assert "只能给出类型化提案" not in zh("live.kernel_note")
    assert zh("live.kernel_note").startswith("模型没有执行权限；仅通过类型和策略校验的提案")
    assert "不显示、不记录、不写入日志" not in zh("live.no_secret")
    for item in DEPLOYMENTS:
        note = UI_MESSAGES[item.price_note]
        assert "2026-10-25" not in note["zh-CN"] and "官方价表" not in note["zh-CN"]
        if item.provider == "zhipu":
            assert "未经核验" in note["zh-CN"] and "unverified" in note["en"]
    assert "若确为免费档" in zh("live.price_note.zhipu-glm-4.7-flash")
    for key in (
        "live.badge.live_revoked",
        "live.reason.revoked_pending",
        "live.reason.ledger_unavailable",
        "live.reason_of.ledger-unavailable",
        "live.reason_of.live-revoked",
        "live.note.settlement_failed",
        "live.note.overrun",
        "live.judge_strip_revoked",
        "live.config_invalid_live",
        "live.client_failed_returned",
        "live.revoked.credentials",
    ):
        assert set(UI_MESSAGES[key]) == {"en", "zh-CN"}, key
    assert set(live_controls.FAILURE_REASONS) >= {"ledger-unavailable", "live-revoked"}
    assert GENERIC
