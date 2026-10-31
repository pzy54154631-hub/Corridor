"""S7 on the web: caps, attribution and failures behave, beyond the text (design §3.5.3).

Text alignment cannot show that a cap holds or that a failure is named after the call
that caused it, so these tests drive web sessions through the adapter as the handlers do
(``sync`` around each business POST) and check the planner each reply was served by, the
counts and spend shared through one journal directory (two processes racing for the last
reservation included), and the source named for every kind of failure. The transport is
the test-only fake model; nothing leaves the machine. They show the adapter's behaviour
for these runs only: the live session's own reservation and settlement rules are covered
by the Streamlit suite, and nothing here claims that spending can never exceed a cap.
"""

import json
import os
import re
import secrets
import socket
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

import pytest

from corridor.agent.llm import _ENV_FIELDS
from corridor.channels.im import IMAdapter
from corridor.i18n import reset_locale, set_locale
from corridor.ui import live_config, live_runtime
from corridor.ui.audience import reset_audience, set_audience
from corridor.ui.live_config import DEPLOYMENTS
from corridor.ui.live_runtime import JournalClient, LiveSession, SpendLedger, access_proof
from corridor.ui.messages import tr
from corridor.web import errors, live
from corridor.web.state import new_web_session
from tests.unit.test_ui_live_mode import ACCESS_CODE, FAKE_KEY, FakeModel, environ
from tests.unit.test_ui_live_review3 import reachable_text

ROOT = Path(__file__).resolve().parents[2]
GENERIC = "The operation was not completed. Review current facts and retry from a fresh proposal."
LABEL = DEPLOYMENTS[0].label
LOOPBACK = {"127.0.0.1", "::1", "localhost"}
RESERVATION = LiveSession._reservation(DEPLOYMENTS[0])


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    for name in (*_ENV_FIELDS, *live_config.PROCESS_NAMES):
        monkeypatch.delenv(name, raising=False)
    connect = socket.socket.connect

    def loopback_only(self, address):
        if not (isinstance(address, tuple) and address[0] in LOOPBACK):
            raise OSError("test: network disabled")
        return connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", loopback_only)


@pytest.fixture(autouse=True)
def judge():
    audience, locale = set_audience("judge"), set_locale("zh-CN")
    yield
    reset_locale(locale)
    reset_audience(audience)


@pytest.fixture
def hosted(monkeypatch, tmp_path):
    def apply(**extra):
        for name, value in environ(tmp_path, **extra).items():
            monkeypatch.setenv(name, value)
        return tmp_path / "journal"

    return apply


def web_session():
    ws = new_web_session(
        "judge", "zh-CN", browser_id=secrets.token_urlsafe(32), tab_id=secrets.token_urlsafe(24)
    )
    live.on_new(ws)
    if ws.controller.start_session().get("status") in {
        "accepted",
        "logged_in",
        "complete",
        "ready",
    }:
        ws.wealth.start_session()
    return ws


def business(ws, action):
    ws.revision += 1
    live.sync(ws, apply=True)
    try:
        return action()
    finally:
        live.sync(ws, apply=True)


def live_post(ws, action):
    ws.revision += 1
    result = action()
    live.sync(ws, apply=True)
    return result


def send(ws, text):
    """Returns the reply, or None when the operation failed before producing one."""

    def action():
        with live.operation(ws, "domestic") as op:
            response = ws.session.receive(text)
            op.bind(response["response_id"])
            ws.rkey_of.setdefault(response["response_id"], f"R{len(ws.rkey_of) + 1}")
        return response

    try:
        return business(ws, action)
    except errors.WebOperationFailed:
        return None


def confirm(ws):
    rid = ws.session.history[-1]["response_id"]

    def action():
        with live.operation(ws, "domestic", rid):
            IMAdapter(ws.session).click("confirm", rid)

    try:
        business(ws, action)
    except errors.WebOperationFailed:
        pass
    return rid


def stop(ws):
    def action():
        with live.operation(ws, "domestic", bind=False):
            ws.session.receive("stop")

    business(ws, action)


def go_live(ws):
    nonce = live_post(ws, lambda: live.challenge(ws))
    assert live_post(ws, lambda: live.unlock(ws, nonce, access_proof(ACCESS_CODE, nonce))) == (
        "accepted"
    )
    live_post(ws, lambda: live.toggle(ws, True))
    assert isinstance(ws.controller.client, JournalClient)


def failure_line(source):
    return tr("live.failure", source=source, reason=tr("live.reason_of." + source))


def texts(notices):
    return [item["text"] for item in notices]


# --- each reply names the planner that actually served it ------------------------------------


def test_each_reply_names_the_planner_that_served_it(hosted, monkeypatch):
    fake = FakeModel(monkeypatch)
    hosted()
    ws = web_session()
    scripted = ws.controller.client
    first = send(ws, "查一下余额")
    go_live(ws)
    second = send(ws, "查一下余额")
    third = send(ws, "给妈妈转800元")
    assert [entry["planner"] for entry in ws.op_log] == ["scripted", LABEL, LABEL]
    assert [entry["rkey"] for entry in ws.op_log] == ["R1", "R2", "R3"]
    assert (first["planner_mode"], second["planner_mode"]) == ("scripted", "record")
    assert ws.op_log[1]["summary"]["calls"] == 1 and ws.op_log[2]["summary"]["calls"] == 2
    assert ws.live_notes[("domestic", third["response_id"])]["recorded"] == 2
    # Switched off at the process mid-transaction: the confirm reaches no model at all and
    # is named as a stopped live client, on that reply only.
    attempts = fake.attempts
    monkeypatch.setenv(live_config.SWITCH, "0")
    rid = confirm(ws)
    assert fake.attempts == attempts and len(ws.op_log) == 3
    assert ws.failures[("domestic", rid)]["source"] == "live-revoked"
    assert failure_line("live-revoked") in live.failure_vm(ws, "domestic", rid)["lines"]
    assert live.failure_vm(ws, "domestic", first["response_id"]) is None
    assert live.badge(ws)["text"] == tr("live.badge.live_revoked", deployment=LABEL)
    # Back at the boundary the very scripted client returns, and so does the badge.
    stop(ws)
    assert ws.controller.client is scripted and ws.session.client is scripted
    monkeypatch.setenv(live_config.SWITCH, "1")
    fourth = send(ws, "查一下余额")
    assert fourth["planner_mode"] == "scripted" and ws.op_log[-1]["planner"] == "scripted"
    # Switching on again needs the access code again (the grant ended with the switch).
    assert live.view(ws)["reason_text"] == tr("live.reason.access_required")
    assert fake.attempts == attempts


def test_a_cap_reached_at_the_boundary_returns_to_scripted_once(hosted, monkeypatch):
    FakeModel(monkeypatch)
    hosted(**{live_config.SESSION_CALLS: "1"})
    ws = web_session()
    scripted = ws.controller.client
    go_live(ws)
    reply = send(ws, "查一下余额")
    assert reply["planner_mode"] == "record" and ws.op_log[-1]["planner"] == LABEL
    notice = tr("live.exhausted_notice", reason=tr("live.exhausted.calls"))
    assert texts(live.page_failure(ws)) == [notice]
    assert live.view(ws)["notice"] == notice
    assert ws.controller.client is scripted and ws.live_toggle is False
    assert live.view(ws)["reason_text"] == tr("live.reason.budget_exhausted")
    # Said once: the next request is scripted and silent about it.
    reply = send(ws, "查一下余额")
    assert reply["planner_mode"] == "scripted" and ws.op_log[-1]["planner"] == "scripted"
    assert live.page_failure(ws) == [] and live.view(ws)["notice"] is None


def test_a_spend_cap_reached_at_the_boundary_says_spend(hosted, monkeypatch):
    FakeModel(monkeypatch, cost=RESERVATION)
    hosted(**{live_config.BUDGET_USD: str(RESERVATION * 3 / 2)})
    ws = web_session()
    go_live(ws)
    send(ws, "查一下余额")
    notice = tr("live.exhausted_notice", reason=tr("live.exhausted.spend"))
    assert texts(live.page_failure(ws)) == [notice]
    assert ws.controller.client is ws.scripted_client


def test_a_cap_reached_mid_transaction_says_what_happens_next(hosted, monkeypatch):
    FakeModel(monkeypatch)
    hosted(**{live_config.SESSION_CALLS: "2"})
    ws = web_session()
    go_live(ws)
    send(ws, "给妈妈转800元")
    assert ws.controller.at_transaction_boundary() is False
    assert live.badge(ws)["text"] == tr("live.badge.live_exhausted", deployment=LABEL)
    assert live.view(ws)["reason_text"] == tr("live.reason.budget_pending")
    assert isinstance(ws.controller.client, JournalClient)
    rid = confirm(ws)
    assert ws.failures[("domestic", rid)]["source"] == "budget-rejected"
    assert ws.controller.gateway.bank.execute_count == 0
    stop(ws)
    assert ws.controller.client is ws.scripted_client
    notice = tr("live.exhausted_notice", reason=tr("live.exhausted.calls"))
    assert notice in texts(live.page_failure(ws))


# --- one journal directory, shared ----------------------------------------------------------


def test_two_sessions_count_calls_apart_and_spend_together(hosted, monkeypatch):
    FakeModel(monkeypatch, cost=Fraction(3, 100))
    hosted(**{live_config.BUDGET_USD: "1/2"})
    first, second = web_session(), web_session()
    go_live(first)
    send(first, "查一下余额")
    go_live(second)
    send(second, "查一下余额")
    send(second, "查一下余额")
    assert first.live.snapshot()["calls"] == 1 and second.live.snapshot()["calls"] == 2
    for ws in (first, second):
        assert ws.live.snapshot()["remaining"]["USD"] == str(Fraction(1, 2) - Fraction(9, 100))
    assert first.live.snapshot()["journal_dirs"] != second.live.snapshot()["journal_dirs"]


RACER = r"""
import json, secrets, sys
from fractions import Fraction
import pytest
from corridor.i18n import set_locale
from corridor.ui.audience import set_audience
from corridor.ui.live_runtime import access_proof
from corridor.web import errors, live
from corridor.web.state import new_web_session
from tests.unit.test_ui_live_mode import ACCESS_CODE, FakeModel

set_audience("judge")
set_locale("zh-CN")
patch = pytest.MonkeyPatch()
FakeModel(patch, cost=Fraction(sys.argv[1]))
ws = new_web_session("judge", "zh-CN", browser_id=secrets.token_urlsafe(32),
                     tab_id=secrets.token_urlsafe(24))
live.on_new(ws)
ws.controller.start_session()
nonce = live.challenge(ws)
assert live.unlock(ws, nonce, access_proof(ACCESS_CODE, nonce)) == "accepted"
live.toggle(ws, True)
live.sync(ws, apply=True)
print("ready", flush=True)
sys.stdin.readline()
ws.revision += 1
live.sync(ws, apply=True)
try:
    with live.operation(ws, "domestic") as op:
        reply = ws.session.receive("查一下余额")
        op.bind(reply["response_id"])
    planner = ws.op_log[-1]["planner"]
except errors.WebOperationFailed:
    planner = None
live.sync(ws, apply=True)
print(json.dumps({"planner": planner, "notices": [n["text"] for n in live.page_failure(ws)],
                  "spent": ws.live.snapshot()["spent"]["USD"]}), flush=True)
"""


def test_two_processes_race_for_the_last_reservation(hosted):
    # The cap holds one reservation and a half, and each call settles at its full
    # reservation: whoever reserves first is served live, the other is not.
    journal = hosted(**{live_config.BUDGET_USD: str(RESERVATION * 3 / 2)})
    env = {name: value for name, value in os.environ.items() if name not in _ENV_FIELDS}
    racers = [
        subprocess.Popen(
            [sys.executable, "-c", RACER, str(RESERVATION)],
            cwd=ROOT,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        for _ in range(2)
    ]
    try:
        for racer in racers:
            assert racer.stdout.readline().strip() == "ready", racer.stderr.read()
        for racer in racers:
            racer.stdin.write("go\n")
            racer.stdin.flush()
        results = [
            json.loads(racer.communicate(timeout=120)[0].splitlines()[-1]) for racer in racers
        ]
    finally:
        for racer in racers:
            if racer.poll() is None:
                racer.kill()
    planners = sorted(str(result["planner"]) for result in results)
    assert planners.count(LABEL) == 1, results
    loser = next(result for result in results if result["planner"] != LABEL)
    # The other one either lost the reservation itself or found the cap at its boundary.
    assert loser["planner"] in {None, "scripted"}
    if loser["planner"] is None:
        assert failure_line("budget-rejected") in loser["notices"]
    else:
        assert tr("live.exhausted_notice", reason=tr("live.exhausted.spend")) in loser["notices"]
    ledger = SpendLedger(journal)
    assert ledger.remaining("USD", RESERVATION * 3 / 2) == RESERVATION / 2


# --- every kind of outcome is named after its own calls --------------------------------------


def changed_handle(step, output):
    if step.startswith("plan:"):
        output = dict(output)
        output["amount"] = {**output["amount"], "ref": "INPUT-FORGED"}
    return output


def bad_intent_schema(step, output):
    if step.startswith("intent:"):
        return {"intent": "transfer", "needs_clarification": "not-a-boolean"}
    return output


@pytest.mark.parametrize(
    ("behaviour", "rule", "source"),
    [
        ("bad", None, "output-rejected"),
        ("error", None, "live-error"),
        ("ok", bad_intent_schema, "agent-rejected"),
    ],
)
def test_a_send_that_fails_live_is_a_page_notice_only(hosted, monkeypatch, behaviour, rule, source):
    hosted()
    ws = web_session()
    go_live(ws)
    FakeModel(monkeypatch, behaviour=behaviour, rule=rule)
    assert send(ws, "给妈妈转800元") is None
    notices = live.page_failure(ws)
    assert texts(notices) == [tr(GENERIC), failure_line(source)]
    assert [item["level"] for item in notices] == ["error", "error"]
    assert ws.failures == {} and ws.op_log == []
    # The live client stays: a model failure never falls back to a scripted reply.
    assert isinstance(ws.controller.client, JournalClient)
    set_audience("customer")
    FakeModel(monkeypatch, behaviour=behaviour, rule=rule)
    send(ws, "给妈妈转800元")
    plain = tr("live.customer.failure", reason=tr("live.reason_of." + source))
    assert texts(live.page_failure(ws))[-1] == plain and source not in plain


def test_a_local_error_before_any_call_is_not_put_on_the_model(hosted, monkeypatch):
    fake = FakeModel(monkeypatch)
    hosted()
    ws = web_session()
    go_live(ws)
    assert send(ws, "转" * 4001) is None
    assert texts(live.page_failure(ws)) == [tr(GENERIC)]
    assert fake.attempts == 0 and ws.failures == {}


def test_a_refused_plan_at_confirm_is_named_on_its_reply(hosted, monkeypatch):
    FakeModel(monkeypatch, rule=changed_handle)
    hosted()
    ws = web_session()
    go_live(ws)
    reply = send(ws, "给妈妈转800元")
    rid = confirm(ws)
    assert rid == reply["response_id"]
    assert ws.failures[("domestic", rid)]["source"] == "agent-rejected"
    block = live.failure_vm(ws, "domestic", rid)
    assert block["lines"][:2] == [tr(GENERIC), failure_line("agent-rejected")]
    assert block["cancel_hint"] == tr("live.failure_cancel")
    assert ws.controller.gateway.bank.execute_count == 0
    # Cancel withdraws it without any model call or note, and reopens the boundary.
    notes_before = dict(ws.live_notes)
    stop(ws)
    assert ws.controller.at_transaction_boundary() is True and ws.live_notes == notes_before


def test_partly_scripted_settlement_and_overrun_are_noted_on_the_reply(hosted, monkeypatch):
    hosted()
    ws = web_session()
    go_live(ws)
    FakeModel(monkeypatch, plan=["timeout", "ok"])
    reply = send(ws, "给妈妈转800元")
    lines = texts(live.notes(ws, reply, "domestic"))
    assert lines == [
        tr("live.note.partly_scripted", count=1),
        tr("live.note.calls", calls=2, recorded=1, timeouts=1, rejected=0),
    ]
    stop(ws)
    original = SpendLedger.reserve

    def reserve(self, *args, **kwargs):
        original(self, *args, **kwargs)

        def settle(actual):
            raise live_runtime.LedgerUnavailable("test: settlement refused")

        return settle

    monkeypatch.setattr(SpendLedger, "reserve", reserve)
    FakeModel(monkeypatch)
    reply = send(ws, "查一下余额")
    assert texts(live.notes(ws, reply, "domestic"))[0] == tr("live.note.settlement_failed")
    set_audience("customer")
    assert tr("live.note.settlement_failed") not in texts(live.notes(ws, reply, "domestic"))
    set_audience("judge")
    monkeypatch.setattr(SpendLedger, "reserve", original)
    # Usage beyond the pre-send bound: noted, and the session stops live calls.
    FakeModel(monkeypatch, cost=RESERVATION + Fraction(1, 100))
    reply = send(ws, "查一下余额")
    assert tr("live.note.overrun") in texts(live.notes(ws, reply, "domestic"))
    assert ws.controller.client is ws.scripted_client
    assert tr("live.exhausted_notice", reason=tr("live.exhausted.spend")) in texts(
        live.page_failure(ws)
    )


def test_wealth_and_clarify_replies_from_the_live_planner_say_so(hosted, monkeypatch):
    FakeModel(monkeypatch)
    hosted()
    ws = web_session()
    go_live(ws)
    reply = send(ws, "帮我理财")
    assert reply["intent"] == "investment"
    assert tr("live.note.wealth_scripted") in texts(live.notes(ws, reply, "domestic"))
    stop(ws)
    reply = send(ws, "给Lin转800元")
    assert reply["status"] == "clarify"
    assert tr("live.note.clarify") in texts(live.notes(ws, reply, "domestic"))
    set_audience("customer")
    assert tr("live.note.clarify") not in texts(live.notes(ws, reply, "domestic"))


def test_no_key_code_or_proof_is_reachable_after_any_outcome(hosted, monkeypatch):
    hosted()
    ws = web_session()
    proofs = []
    for code in ("wrong-code", ACCESS_CODE):
        nonce = live_post(ws, lambda: live.challenge(ws))
        proofs.append(access_proof(code, nonce))
        live_post(ws, lambda nonce=nonce, proof=proofs[-1]: live.unlock(ws, nonce, proof))
    live_post(ws, lambda: live.toggle(ws, True))
    for behaviour in ("ok", "bad", "error", "timeout"):
        FakeModel(monkeypatch, behaviour=behaviour)
        send(ws, "给妈妈转800元")
        confirm(ws)
        stop(ws)
    shown = json.dumps(
        [live.view(ws), live.badge(ws), live.corner(ws), live.judge_strip(ws)],
        ensure_ascii=False,
    )
    found = reachable_text(ws) + [shown]
    for secret in (FAKE_KEY, ACCESS_CODE, "wrong-code", *proofs):
        assert not [text for text in found if secret in text]
    assert not re.search(r"\b[0-9a-f]{64}\b", shown)
