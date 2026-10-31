"""S7 on the web: the live adapter (``corridor.web.live``) over one web session.

The ``v0`` tests pin the scripted pass-through every other line builds on (the process
switch off). The others drive the switch with the Streamlit suite's test-only fake model
inside the client: no request is ever sent, and sockets beyond loopback are refused.
"""

import ast
import importlib
import re
import secrets
import socket
import sys
import types
from pathlib import Path

import pytest

from corridor.agent.llm import _ENV_FIELDS
from corridor.i18n import reset_locale, set_locale
from corridor.ui import live_config, live_runtime
from corridor.ui.audience import reset_audience, set_audience
from corridor.ui.live_config import DEPLOYMENTS
from corridor.ui.live_runtime import JournalClient, access_proof
from corridor.ui.messages import tr
from corridor.web import live
from corridor.web.state import new_web_session
from tests.unit.test_ui_live_mode import ACCESS_CODE, FAKE_KEY, FakeModel, environ
from tests.unit.test_ui_live_review3 import reachable_text

LIVE_PY = Path(live.__file__)
GENERIC = "The operation was not completed. Review current facts and retry from a fresh proposal."
CJK = re.compile(r"[\u3000-\u303f\u3400-\u4dbf\u4e00-\u9fff\uff00-\uffef]")
LABEL = DEPLOYMENTS[0].label
LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def _stand_in_errors():
    """The shell line's error table as the contract (§6.3) describes it."""
    module = types.ModuleType("corridor.web.errors")

    class WebError(Exception):
        code, status, with_state = "internal", 500, False

        def __init__(self, code=None):
            super().__init__(code or self.code)
            if code:
                self.code = code

    class WebOperationFailed(WebError):
        code, status, with_state = "operation_failed", 422, True

    class WebInvariantError(WebError):
        pass

    module.WebError, module.WebOperationFailed = WebError, WebOperationFailed
    module.WebInvariantError, module.error = WebInvariantError, WebError
    return module


@pytest.fixture(autouse=True)
def errors(monkeypatch):
    try:
        return importlib.import_module("corridor.web.errors")
    except ImportError:
        module = _stand_in_errors()
        monkeypatch.setitem(sys.modules, "corridor.web.errors", module)
        return module


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """Live variables come only from each test; nothing leaves the machine."""
    for name in (*_ENV_FIELDS, *live_config.PROCESS_NAMES):
        monkeypatch.delenv(name, raising=False)
    connect = socket.socket.connect

    def loopback_only(self, address):
        if not (isinstance(address, tuple) and address[0] in LOOPBACK):
            raise OSError("test: network disabled")
        return connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", loopback_only)


@pytest.fixture
def audience():
    """What the executor sets in its thread for each request: locale and audience."""
    tokens = []

    def use(view, locale="zh-CN"):
        tokens.append((set_audience(view), set_locale(locale)))

    yield use
    for view, locale in reversed(tokens):
        reset_locale(locale)
        reset_audience(view)


@pytest.fixture
def hosted(monkeypatch, tmp_path):
    """The seven process variables of a hosted live demo (synthetic credential file)."""

    def apply(**extra):
        for name, value in environ(tmp_path, **extra).items():
            monkeypatch.setenv(name, value)
        return tmp_path / "journal"

    return apply


def web_session(view="judge", locale="zh-CN", *, login=True):
    ws = new_web_session(
        view, locale, browser_id=secrets.token_urlsafe(32), tab_id=secrets.token_urlsafe(24)
    )
    live.on_new(ws)
    if login:
        result = ws.controller.start_session()
        if result.get("status") in {"accepted", "logged_in", "complete", "ready"}:
            ws.wealth.start_session()
    return ws


def business(ws, action):
    """A business POST (design §2.3): the switch is applied before and after it."""
    ws.revision += 1
    live.sync(ws, apply=True)
    try:
        return action()
    finally:
        live.sync(ws, apply=True)


def live_post(ws, action):
    """A live POST: the action, then the switch applied."""
    ws.revision += 1
    result = action()
    live.sync(ws, apply=True)
    return result


def send(ws, text, prefix="domestic"):
    def action():
        with live.operation(ws, prefix) as op:
            response = ws.session.receive(text)
            op.bind(response["response_id"])
            ws.rkey_of.setdefault(response["response_id"], f"R{len(ws.rkey_of) + 1}")
        return response

    return business(ws, action)


def stop(ws):
    def action():
        with live.operation(ws, "domestic", bind=False):
            ws.controller.stop()
            ws.session.withdraw_pending()

    business(ws, action)


def unlock(ws, code=ACCESS_CODE):
    nonce = live_post(ws, lambda: live.challenge(ws))
    assert re.fullmatch(r"[0-9a-f]{32}", nonce)
    return live_post(ws, lambda: live.unlock(ws, nonce, access_proof(code, nonce)))


def switch(ws, on):
    live_post(ws, lambda: live.toggle(ws, on))


def code_of(error):
    return getattr(error, "code", None)


# --- v0: the scripted pass-through (the switch off) ---------------------------------------


def test_v0_new_session_keeps_the_scripted_client_and_the_switch_off(tmp_path, monkeypatch):
    # A credential directory and a journal directory alone change nothing.
    monkeypatch.setenv(live_config.ENV_DIR, str(tmp_path / "creds"))
    monkeypatch.setenv(live_config.JOURNAL_DIR, str(tmp_path / "journal"))
    ws = web_session()
    client = ws.controller.client
    assert ws.scripted_client is client and client.mode == "scripted"
    for apply in (False, True):
        assert live.sync(ws, apply=apply) == {
            "state": "OFF",
            "enabled": False,
            "invalid": False,
            "boundary": True,
        }
    assert live.may_call(ws) is False
    response = send(ws, "给妈妈转500元")
    assert response["planner_mode"] == "scripted"
    assert ws.controller.client is client and ws.session.client is client
    assert ws.live is None and not (tmp_path / "journal").exists()
    replacement = web_session(login=False)
    live.on_reset(ws, replacement)
    live.close(replacement)
    assert replacement.live is None and replacement.live_toggle is False


def test_v0_operation_names_the_scripted_planner_for_its_reply():
    ws = web_session(login=False)
    with live.operation(ws, "domestic") as op:
        op.bind("RESP-1")
    with live.operation(ws, "domestic", "RESP-1", bind=False):
        pass
    with live.operation(ws, "domestic"):
        pass
    assert ws.op_log == [{"rkey": None, "rid": "RESP-1", "planner": "scripted", "summary": None}]
    # A reply's key registered after the operation returned is filled in by the next sync.
    ws.rkey_of["RESP-1"] = "R1"
    live.sync(ws, apply=True)
    assert ws.op_log[0]["rkey"] == "R1"
    with live.operation(ws, "domestic", "RESP-1"):
        pass
    assert [entry["rkey"] for entry in ws.op_log] == ["R1", "R1"]
    assert ws.failures == {} and ws.pending_notices == [] and ws.live_notes == {}


def test_v0_failure_on_a_reply_closes_its_card(errors, audience):
    ws = web_session(login=False)
    for view in ("judge", "customer"):
        audience(view)
        assert live.failure_vm(ws, "domestic", "RESP-2") is None
    with pytest.raises(errors.WebOperationFailed):
        with live.operation(ws, "domestic", "RESP-2"):
            raise RuntimeError("secret-path C:\\x")
    assert ws.failures == {("domestic", "RESP-2"): {"live": None, "source": None}}
    assert ws.op_log == [] and ws.pending_notices == []
    for view in ("judge", "customer"):
        audience(view)
        failure = live.failure_vm(ws, "domestic", "RESP-2")
        assert failure == {
            "kind": "failed",
            "lines": [tr(GENERIC)],
            "cancel_hint": tr("live.failure_cancel"),
        }
        assert "secret" not in repr(failure)


def test_v0_failure_before_any_reply_is_a_page_notice(errors, audience):
    audience("judge")
    ws = web_session(login=False)
    for kind in (ValueError, KeyError, TimeoutError, OSError):
        with pytest.raises(errors.WebOperationFailed):
            with live.operation(ws, "domestic") as op:
                raise kind("secret")
        assert op.rid is None
    assert ws.failures == {} and ws.op_log == []
    notices = live.page_failure(ws)
    assert [item["level"] for item in notices] == ["error"] * 4
    assert {item["text"] for item in notices} == {tr(GENERIC)}
    assert len({item["id"] for item in notices}) == 4
    assert all(item["id"].startswith(ws.generation + ":") for item in notices)
    assert ws.pending_notices == [] and live.page_failure(ws) == []


def test_v0_other_exceptions_pass_through_unrecorded():
    ws = web_session(login=False)
    with pytest.raises(TypeError):
        with live.operation(ws, "domestic", "RESP-3"):
            raise TypeError("unexpected")
    assert ws.failures == {} and ws.op_log == [] and ws.pending_notices == []


def test_v0_badge_corner_strip_and_notes(audience):
    ws = web_session(login=False)
    audience("judge")
    assert live.badge(ws) == {"kind": "demo", "text": tr("live.badge.scripted")}
    assert live.corner(ws) == tr("simulation_notice")
    assert live.judge_strip(ws) == tr("polish.view.judge_strip")
    assert live.counters(ws) == [] and live.scope_note(ws, "wealth") is None
    audience("customer")
    assert live.judge_strip(ws) is None
    assert live.notes(ws, {"response_id": "RESP-1"}, "domestic") == []


def test_v0_panel_with_the_switch_off_shows_the_badge_and_the_off_line(audience):
    ws = web_session(login=False)
    nullable = (
        "selected",
        "revoked_text",
        "reason_text",
        "boundary_text",
        "caps_text",
        "kernel_note",
        "no_secret",
        "scope_note",
        "notice",
    )
    for view in ("customer", "judge"):
        audience(view)
        panel = live.view(ws)
        assert panel["enabled"] is False and panel["config_invalid"] is False
        # As the Streamlit page: the judge view adds the off hint, the customer view only
        # has the badge.
        assert panel["off_text"] == (tr("live.off_hint") if view == "judge" else None)
        assert panel["deployments"] == [] and panel["boundary"] is True
        assert panel["toggle"] == {"on": False, "disabled": True, "label": tr("live.toggle")}
        assert panel["unlock"]["disabled"] is True and panel["unlock"]["result_text"] is None
        assert all(panel[key] is None for key in nullable)
        assert (
            panel["badge"] == live.badge(ws) == {"kind": "demo", "text": tr("live.badge.scripted")}
        )
        # Judge-only keys are absent rather than null.
        assert "price" not in panel and "counters" not in panel
        assert panel["title"] == tr("live.expander")
        assert panel["intro"] == tr("live.intro" if view == "judge" else "live.customer.intro")
    assert ws.live is None


def test_v0_source_has_no_cjk_wallclock_or_cap_literals():
    source = LIVE_PY.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert not CJK.search(node.value)
        if isinstance(node, ast.Constant) and isinstance(node.value, int):
            assert node.value not in {16384, 8192}
    assert "16384" not in source and "8192" not in source
    for name in ("datetime", "time.time", "monotonic", "perf_counter", "print(", "logging"):
        assert name not in source
    imported = {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module
    }
    # Everything from ui/ and agent/ comes through the compat surface.
    assert not {name for name in imported if name.startswith(("corridor.ui", "corridor.agent"))}


# --- v1: live POSTs ---------------------------------------------------------------------------


def test_live_posts_need_the_process_switch(errors):
    ws = web_session()
    for action in (
        lambda: live.challenge(ws),
        lambda: live.unlock(ws, "0" * 32, "0" * 64),
        lambda: live.select(ws, DEPLOYMENTS[1].id),
        lambda: live.toggle(ws, True),
    ):
        with pytest.raises(errors.WebError) as caught:
            action()
        assert code_of(caught.value) == "live_unavailable"
    assert ws.live is None and ws.live_toggle is False


def test_the_panel_before_any_post_reads_a_fresh_session_and_keeps_none(hosted, audience):
    journal = hosted()
    ws = web_session()
    audience("judge")
    panel = live.view(ws)
    assert ws.live is None
    assert panel["enabled"] is True and panel["off_text"] is None
    assert panel["reason_text"] == tr("live.reason.access_required")
    assert panel["unlock"]["disabled"] is False and panel["toggle"]["disabled"] is True
    assert panel["selected"] == DEPLOYMENTS[0].id
    assert [item["id"] for item in panel["deployments"]] == [item.id for item in DEPLOYMENTS]
    assert panel["deployments"][0]["credential_text"] == tr("live.credential.ok")
    assert panel["price"].startswith(DEPLOYMENTS[0].model)
    assert panel["caps_text"] and panel["counters"] and panel["badge"]["kind"] == "demo"
    # Only the judge view names credential states, prices and counters.
    audience("customer")
    panel = live.view(ws)
    assert "price" not in panel and "counters" not in panel
    assert all("credential_text" not in item for item in panel["deployments"])
    assert panel["reason_text"] == tr("live.reason.access_required")
    assert panel["caps_text"] == tr("live.customer.caps", calls=live_config.DEFAULT_SESSION_CALLS)
    assert ws.live is None and not list(journal.glob("*/session-*"))


def test_unlocking_and_switching_both_ways_at_the_boundary(hosted, audience, monkeypatch):
    FakeModel(monkeypatch)
    hosted()
    audience("judge")
    ws = web_session()
    scripted = ws.controller.client
    assert unlock(ws, "wrong-code") == "invalid"
    assert live.view(ws)["unlock"]["result_text"] == tr("live.denied")
    assert ws.controller.client is scripted
    assert unlock(ws) == "accepted"
    panel = live.view(ws)
    assert panel["unlock"]["result_text"] == tr("live.unlocked")
    assert panel["toggle"] == {"on": False, "disabled": False, "label": tr("live.toggle")}
    assert panel["reason_text"] == tr("live.reason.ready")
    # The result is said once: the next request no longer shows it.
    ws.revision += 1
    assert live.view(ws)["unlock"]["result_text"] is None
    switch(ws, True)
    client = ws.controller.client
    assert isinstance(client, JournalClient) and ws.session.client is client
    assert ws.controller.chat.client is client and live.may_call(ws) is True
    assert live.sync(ws, apply=False)["state"] == "LIVE"
    assert live.badge(ws) == {"kind": "model", "text": tr("live.badge.live", deployment=LABEL)}
    assert live.corner(ws) == tr("simulation_notice") + " · " + tr("live.corner", deployment=LABEL)
    assert live.judge_strip(ws) == tr("live.judge_strip", deployment=LABEL)
    panel = live.view(ws)
    assert panel["title"] == tr("live.expander_on", deployment=LABEL)
    assert panel["reason_text"] == tr("live.reason.on", deployment=LABEL)
    assert panel["toggle"]["on"] is True
    response = send(ws, "查一下余额")
    assert response["planner_mode"] == "record"
    entry = ws.op_log[-1]
    assert entry["planner"] == LABEL and entry["summary"]["calls"] >= 1
    notes = live.notes(ws, response, "domestic")
    assert [note["text"] for note in notes] == [
        tr(
            "live.note.calls",
            calls=entry["summary"]["calls"],
            recorded=entry["summary"]["recorded"],
            timeouts=0,
            rejected=0,
        )
    ]
    audience("customer")
    assert live.notes(ws, response, "domestic") == []
    switch(ws, False)
    # Off again: the very scripted object the session was built with is restored.
    assert ws.controller.client is scripted and ws.session.client is scripted
    assert live.badge(ws) == {"kind": "demo", "text": tr("live.badge.scripted")}
    assert live.sync(ws, apply=False)["state"] == "SCRIPTED"
    response = send(ws, "查一下余额")
    assert response["planner_mode"] == "scripted" and ws.op_log[-1]["planner"] == "scripted"


def test_a_challenge_is_single_use_and_no_code_or_proof_is_kept(hosted, monkeypatch):
    FakeModel(monkeypatch)
    hosted()
    ws = web_session()
    nonce = live.challenge(ws)
    proof = access_proof(ACCESS_CODE, nonce)
    assert live.challenge(ws) == nonce
    assert live.unlock(ws, nonce, proof) == "accepted"
    # The same proof again is stale (the challenge is used up), not a wrong code.
    assert live.unlock(ws, nonce, proof) == "stale"
    assert ws.live.snapshot()["locked"] is False and live.challenge(ws) != nonce
    switch(ws, True)
    send(ws, "查一下余额")
    texts = reachable_text(ws)
    for secret in (ACCESS_CODE, proof, FAKE_KEY):
        assert not [text for text in texts if secret in text]


def test_wrong_codes_lock_the_session_and_disable_unlock(hosted, audience):
    hosted()
    audience("judge")
    ws = web_session()
    for _ in range(live_runtime.LOCK_AFTER_FAILURES):
        assert unlock(ws, "nope") == "invalid"
    assert unlock(ws) == "locked"
    panel = live.view(ws)
    assert panel["unlock"]["disabled"] is True
    assert panel["unlock"]["result_text"] == tr("live.locked_out")
    assert panel["reason_text"] == tr("live.reason.locked")
    assert panel["toggle"]["disabled"] is True


def test_switching_waits_for_the_transaction_boundary(hosted, audience, errors, monkeypatch):
    FakeModel(monkeypatch)
    hosted()
    audience("judge")
    ws = web_session()
    unlock(ws)
    switch(ws, True)
    client = ws.controller.client
    send(ws, "给妈妈转5000元")
    assert ws.controller.at_transaction_boundary() is False
    nonce = live.challenge(ws)
    for action, code in (
        (lambda: live.toggle(ws, False), "not_at_boundary"),
        (lambda: live.unlock(ws, nonce, access_proof(ACCESS_CODE, nonce)), "not_at_boundary"),
        (lambda: live.select(ws, DEPLOYMENTS[1].id), "not_at_boundary"),
    ):
        with pytest.raises(errors.WebError) as caught:
            action()
        assert code_of(caught.value) == code
    # A refused unlock leaves the challenge unused.
    assert live.challenge(ws) == nonce and ws.controller.client is client
    panel = live.view(ws)
    assert panel["boundary"] is False and panel["boundary_text"] == tr("live.boundary_locked")
    assert panel["toggle"]["disabled"] is True and panel["unlock"]["disabled"] is True
    stop(ws)
    assert ws.controller.at_transaction_boundary() is True
    with pytest.raises(errors.WebError) as caught:
        live.select(ws, DEPLOYMENTS[1].id)
    assert code_of(caught.value) == "live_on"
    switch(ws, False)
    with pytest.raises(errors.WebError) as caught:
        live.select(ws, "no-such-deployment")
    assert code_of(caught.value) == "invalid_input"
    live_post(ws, lambda: live.select(ws, DEPLOYMENTS[1].id))
    assert live.view(ws)["selected"] == DEPLOYMENTS[1].id


def test_a_reset_moves_the_live_session_and_turns_the_switch_off(hosted, monkeypatch):
    FakeModel(monkeypatch)
    hosted()
    ws = web_session()
    unlock(ws)
    switch(ws, True)
    send(ws, "查一下余额")
    session = ws.live
    calls = session.snapshot()["calls"]
    new = web_session()
    new.scripted_client = None
    live.on_reset(ws, new)
    assert new.live is session and ws.live is None
    assert new.live_toggle is False and ws.live_toggle is False
    assert new.scripted_client is new.controller.client
    # The counts carry over; the old client is let go and refuses any further call.
    assert session.snapshot()["calls"] == calls
    assert not session.is_current(ws.controller.client)
    assert live.sync(new, apply=True)["state"] == "SCRIPTED"
    live.close(new)
    assert new.live_toggle is False


def test_a_missing_scripted_client_fails_closed(hosted, errors, monkeypatch):
    FakeModel(monkeypatch)
    hosted()
    ws = web_session()
    unlock(ws)
    switch(ws, True)
    ws.scripted_client = None
    ws.live_toggle = False
    with pytest.raises(errors.WebInvariantError):
        live.sync(ws, apply=True)
    # No new scripted client was made in its place; the switch stays off.
    assert isinstance(ws.controller.client, JournalClient) and ws.live_toggle is False
    assert not ws.live.is_current(ws.controller.client)


def test_an_invalid_configuration_is_named_and_stops_the_live_client(hosted, audience, monkeypatch):
    FakeModel(monkeypatch)
    hosted()
    audience("judge")
    ws = web_session()
    unlock(ws)
    switch(ws, True)
    send(ws, "给妈妈转800元")
    monkeypatch.setenv(live_config.SESSION_CALLS, "two")
    state = live.sync(ws, apply=True)
    assert state == {"state": "OFF", "enabled": False, "invalid": True, "boundary": False}
    panel = live.view(ws)
    assert panel["config_invalid"] is True and panel["enabled"] is False
    assert panel["off_text"] == tr("live.config_invalid_live")
    assert panel["reason_text"] == tr("live.reason.revoked_pending")
    assert live.badge(ws)["text"] == tr("live.badge.live_revoked", deployment=LABEL)
    assert live.corner(ws) == tr("simulation_notice")
    assert live.judge_strip(ws) == tr("live.judge_strip_revoked", deployment=LABEL)
    stop(ws)
    assert ws.controller.client is ws.scripted_client
    panel = live.view(ws)
    assert (panel["off_text"], panel["reason_text"]) == (
        tr("live.config_invalid"),
        tr("live.off_hint"),
    )
