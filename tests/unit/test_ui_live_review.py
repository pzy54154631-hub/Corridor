"""S7 review fixes: one regression per must item, then the adopted suggestions.

Must items (security M1-M3, honesty M4-M8):
M1 a finished action never holds the transaction boundary (the switch goes both ways, the
   cap falls back after a transfer, the S7 demo path runs end to end);
M2 a live failure at the confirm gesture is shown on the card and can be cancelled,
   never a traceback;
M3 no host clock: the spend cap is cumulative per journal directory;
M4 the judge strip names the live planner instead of claiming "no model calls";
M5 the wealth page's badge describes its scripted advisor;
M6 judge counts and journal directories survive the return to the scripted planner;
M7 one request whose calls were partly answered by scripts says so (per request); a
   transport timeout of every registered deployment is a failure, not a fallback;
M8 the live variables are process environment variables, blank means unset.

No test calls a model: transports are replaced in-process and sockets are refused.
"""

import ast
import contextlib
import inspect
import json
import socket
import sqlite3
from fractions import Fraction
from pathlib import Path

import httpx
import pytest

from corridor.agent.domestic_session import DomesticSession
from corridor.agent.llm import _ENV_FIELDS, BudgetExceeded, LLMConfigurationError, LLMOutputError
from corridor.ui import live_config, live_runtime
from corridor.ui.controller import DemoController, demo_state
from corridor.ui.live_config import DEPLOYMENTS, LiveConfig
from corridor.ui.live_runtime import JournalClient, LiveSession
from corridor.ui.messages import UI_MESSAGES
from tests._ast_checks import WALLCLOCK, Analysis
from tests.e2e.test_demo_ui import app, navigate
from tests.unit.test_ui_live_mode import (
    ACCESS_CODE,
    FAKE_KEY,
    UI,
    FakeModel,
    Owner,
    _access_field,  # noqa: F401 - the autouse fixture that records the access field
    access_field_disabled,
    badge,
    call,
    credential_dir,
    environ,
    journal_records,
    send,
    shown_text,
    start,
    switch,
    toggle_disabled,
    unit_session,
    unlock,
)

ROOT = Path(__file__).resolve().parents[2]
ZH = "zh-CN"


@pytest.fixture(autouse=True)
def _no_inherited_client_settings(monkeypatch):
    """Live access refuses inherited LLM_* settings; a developer shell may export them."""
    for name in _ENV_FIELDS:
        monkeypatch.delenv(name, raising=False)


LABEL = DEPLOYMENTS[0].label
GENERIC = UI_MESSAGES[
    "The operation was not completed. Review current facts and retry from a fresh proposal."
][ZH]


def msg(key, locale=ZH, **values):
    text = UI_MESSAGES[key][locale]
    return text.format(**values) if values else text


def failure_line(source, locale=ZH):
    return msg(
        "live.failure", locale, source=source, reason=msg(f"live.reason_of.{source}", locale)
    )


def response(page, prefix="domestic"):
    session = page.session_state["domestic_session"]
    return session.response(page.session_state[f"{prefix}_response_id"])


def keys(page):
    return {button.key for button in page.button}


def confirm(page, key="domestic_confirm"):
    page.button(key=key).click().run()
    assert not page.exception, [str(item.value) for item in page.exception]
    return page


def settle(page):
    """A transfer reply moves the chat to its own route; AppTest keeps the previous run's
    chat form as stale duplicates until the next full run, so go back to the chat page."""
    navigate(page, "conversation")
    assert not page.exception
    return page


def changed_handle(step, output):
    """The model echoes the plan with a forged signed handle (the planner must refuse)."""
    if step.startswith("plan:"):
        output = dict(output)
        output["amount"] = {**output["amount"], "ref": "INPUT-FORGED"}
    return output


def rejected_at_plan(step, output):
    if step.startswith("plan:"):
        return LLMOutputError("Invalid model response; no replay recorded")
    return output


def bad_intent_schema(step, output):
    """Passes the client, fails the agent's schema check (validate_model_output)."""
    if step.startswith("intent:"):
        return {"intent": "transfer", "needs_clarification": "not-a-boolean"}
    return output


@contextlib.contextmanager
def no_network(monkeypatch):
    def refused(*args, **kwargs):
        raise RuntimeError("test: network disabled")

    for target, name in (
        (socket, "create_connection"),
        (socket, "getaddrinfo"),
        (socket.socket, "connect"),
        (socket.socket, "connect_ex"),
    ):
        monkeypatch.setattr(target, name, refused)
    yield


# --- M1: the transaction boundary -----------------------------------------------------------


def test_a_settled_transfer_reopens_the_boundary_in_scripted_mode(monkeypatch, tmp_path):
    FakeModel(monkeypatch)
    page = start(monkeypatch, tmp_path)
    unlock(page)
    controller = page.session_state["controller"]
    send(page, "给妈妈转800元")
    assert controller.at_transaction_boundary() is False
    confirm(page)
    assert response(page)["status"] == "settled"
    assert controller.gateway.bank.execute_count == 1
    # The settled draft is finished: it no longer holds the boundary.
    assert controller.open_actions() == []
    assert controller.at_transaction_boundary() is True
    assert toggle_disabled(page) is False
    assert msg("live.boundary_locked") not in shown_text(page)
    switch(page, True)
    assert isinstance(controller.client, JournalClient)


def test_a_live_transfer_settles_and_the_switch_returns_to_scripted(monkeypatch, tmp_path):
    FakeModel(monkeypatch)
    page = start(monkeypatch, tmp_path)
    unlock(page)
    switch(page, True)
    controller = page.session_state["controller"]
    scripted = page.session_state["live_scripted_client"]
    send(page, "给妈妈转800元")
    confirm(page)
    assert response(page)["status"] == "settled"
    assert {c["source"] for c in controller.client.calls} == {"live-recorded"}
    assert controller.at_transaction_boundary() is True and toggle_disabled(page) is False
    switch(page, False)
    assert controller.client is scripted
    assert page.session_state["domestic_session"].client is scripted
    assert msg("live.badge.scripted") in badge(page)[-1]


def test_a_cap_reached_by_a_transfer_falls_back_at_the_boundary(monkeypatch, tmp_path):
    FakeModel(monkeypatch)
    # Intent, extract and plan: the third call settles the transfer and reaches the cap.
    page = start(monkeypatch, tmp_path, **{live_config.SESSION_CALLS: "3"})
    unlock(page)
    switch(page, True)
    controller = page.session_state["controller"]
    scripted = page.session_state["live_scripted_client"]
    send(page, "给妈妈转800元")
    assert controller.client.mode == "record"
    confirm(page)
    assert response(page)["status"] == "settled"
    live = page.session_state["live_session"]
    assert live.snapshot()["calls"] == 3 and live.snapshot()["reason"] == "budget_exhausted"
    text = shown_text(page)
    assert msg("live.exhausted_notice", reason=msg("live.exhausted.calls")) in text
    assert controller.client is scripted
    assert page.toggle(key="live_mode").value is False
    assert msg("live.badge.scripted") in badge(page)[-1]
    # The scripted demo keeps working after the fallback.
    send(page, "查一下余额")
    assert response(page)["planner_mode"] == "scripted"


def test_the_s7_demo_path_runs_end_to_end(monkeypatch, tmp_path):
    FakeModel(monkeypatch)
    page = start(monkeypatch, tmp_path)
    controller = page.session_state["controller"]
    assert msg("live.badge.scripted") in badge(page)[-1]
    assert [item.label for item in page.expander if item.label == msg("live.expander")]
    unlock(page)
    switch(page, True)
    assert msg("live.badge.live", deployment=LABEL) in badge(page)[-1]
    assert [
        item.label
        for item in page.expander
        if item.label == msg("live.expander_on", deployment=LABEL)
    ]
    send(page, "给妈妈转800元")
    reply = response(page)
    assert reply["planner_mode"] == "record" and reply["card"]["kind"] == "transfer"
    text = shown_text(page)
    assert msg("live.status").split("{")[0] in text
    assert f"{DEPLOYMENTS[0].id}/session-0001" in text
    confirm(page)
    assert response(page)["status"] == "settled"
    assert controller.gateway.bank.execute_count == 1
    switch(page, False)
    assert msg("live.badge.scripted") in badge(page)[-1]
    assert controller.client.mode == "scripted"
    assert toggle_disabled(page) is False


def test_dead_clarify_drafts_do_not_lock_but_a_pending_recipient_choice_does(monkeypatch, tmp_path):
    FakeModel(monkeypatch)
    page = start(monkeypatch, tmp_path)
    unlock(page)
    switch(page, True)
    controller = page.session_state["controller"]
    # No recipient candidate: this draft can never become a card.
    send(page, "给小王转800元")
    settle(page)
    assert response(page)["status"] == "clarify" and not response(page)["card"]
    assert controller.at_transaction_boundary() is True
    assert toggle_disabled(page) is False
    # Two contacts named Lin: the choice leads to a card the planner confirms.
    send(page, "给Lin转800元")
    settle(page)
    assert response(page)["payload"]["contacts"] == ["CONTACT_LIN_A", "CONTACT_LIN_B"]
    assert controller.at_transaction_boundary() is False
    assert toggle_disabled(page) is True
    assert msg("live.boundary_locked") in shown_text(page)
    page.button(key="stop_all").click().run()
    assert not page.exception
    assert controller.at_transaction_boundary() is True and toggle_disabled(page) is False


def test_rejected_and_pending_actions_are_judged_by_their_state():
    controller = DemoController(initial_state=demo_state(showcase=True))
    controller.start_session()
    session = DomesticSession(controller)
    # The kernel refuses the amount at the device gesture: the action is finished.
    refused = session.receive("给妈妈转9999999元")
    assert controller.at_transaction_boundary() is False
    assert session.confirm(refused["response_id"])["status"] == "rejected"
    assert controller.at_transaction_boundary() is True
    assert controller.gateway.bank.execute_count == 0
    # A red transfer waiting for the simulated SMS is open until it completes or stops.
    red = session.receive("给妈妈转5000元")
    assert session.confirm(red["response_id"])["status"] == "waiting_mfa"
    assert controller.at_transaction_boundary() is False
    controller.stop()
    assert controller.at_transaction_boundary() is True


# --- M2: failures at the confirm gesture ----------------------------------------------------


@pytest.mark.parametrize(
    ("rule", "source"),
    [(changed_handle, "agent-rejected"), (rejected_at_plan, "output-rejected")],
)
def test_a_live_failure_at_confirm_is_shown_and_can_be_cancelled(
    monkeypatch, tmp_path, rule, source
):
    fake = FakeModel(monkeypatch, rule=rule)
    page = start(monkeypatch, tmp_path)
    unlock(page)
    switch(page, True)
    controller = page.session_state["controller"]
    send(page, "给妈妈转800元")
    identifier = page.session_state["domestic_response_id"]
    confirm(page)
    assert any(kwargs["step"].startswith("plan:") for kwargs, _ in fake.requests) or (
        source == "output-rejected"
    )
    text = shown_text(page)
    assert GENERIC in text and failure_line(source) in text
    assert msg("live.failure_cancel") in text
    assert controller.gateway.bank.execute_count == 0
    # The device inputs are consumed: no second confirm, Cancel stays.
    assert "domestic_confirm" not in keys(page) and "domestic_cancel" in keys(page)
    page.run()
    assert not page.exception
    assert failure_line(source) in shown_text(page) and "domestic_confirm" not in keys(page)
    page.button(key="domestic_cancel").click().run()
    assert not page.exception
    session = page.session_state["domestic_session"]
    assert session.response(identifier)["status"] == "withdrawn"
    assert controller.at_transaction_boundary() is True and toggle_disabled(page) is False
    assert controller.client.mode == "record" and controller.gateway.bank.execute_count == 0


def test_a_live_failure_at_the_recipient_choice_confirm_is_shown_and_can_be_cancelled(
    monkeypatch, tmp_path
):
    FakeModel(monkeypatch, rule=changed_handle)
    page = start(monkeypatch, tmp_path)
    unlock(page)
    switch(page, True)
    controller = page.session_state["controller"]
    send(page, "给Lin转800元")
    settle(page)
    page.selectbox(key="domestic_clarify_contact").select("CONTACT_LIN_A").run()
    page.button(key="domestic_select_contact").click().run()
    assert not page.exception and "domestic_selected_confirm" in keys(page)
    confirm(page, "domestic_selected_confirm")
    text = shown_text(page)
    assert GENERIC in text and failure_line("agent-rejected") in text
    assert "domestic_selected_confirm" not in keys(page)
    assert "domestic_selected_cancel" in keys(page)
    assert controller.gateway.bank.execute_count == 0
    page.button(key="domestic_selected_cancel").click().run()
    assert not page.exception
    assert controller.at_transaction_boundary() is True


def test_a_scripted_confirm_failure_is_not_attributed_to_a_model(monkeypatch, tmp_path):
    # Switch off: a failure inside the gesture is still caught and shown generically.
    page = start(monkeypatch, tmp_path)
    controller = page.session_state["controller"]
    send(page, "给妈妈转800元")

    def broken(*args, **kwargs):
        raise ValueError("synthetic workflow failure")

    monkeypatch.setattr(controller.workflow, "confirm_transfer", broken)
    confirm(page)
    text = shown_text(page)
    assert GENERIC in text
    assert msg("live.failure").split("{")[0] not in text
    assert "domestic_confirm" not in keys(page) and "domestic_cancel" in keys(page)


# --- M3: no host clock ----------------------------------------------------------------------


HOST_CLOCK = WALLCLOCK | {
    "time.gmtime",
    "time.localtime",
    "time.ctime",
    "time.asctime",
    "time.strftime",
    "time.monotonic",
    "time.monotonic_ns",
    "time.perf_counter",
    "time.perf_counter_ns",
    "time.process_time",
}


def test_the_ui_package_reads_no_host_clock():
    errors = []
    for path in sorted(UI.rglob("*.py")):
        analysis = Analysis(path.read_text(encoding="utf-8"))
        for node in ast.walk(analysis.tree):
            if isinstance(node, ast.Call) and analysis.name(node.func) in HOST_CLOCK:
                errors.append(f"{path.name}:{node.lineno}: {analysis.name(node.func)}")
    assert not errors, errors
    for name in ("live_config.py", "live_runtime.py", "live_controls.py"):
        source = (UI / name).read_text(encoding="utf-8")
        assert "gmtime" not in source and "strftime" not in source, name


def test_the_spend_cap_is_cumulative_per_journal_directory_without_a_calendar(
    tmp_path, monkeypatch
):
    assert "day" not in inspect.signature(LiveSession).parameters
    assert not hasattr(live_runtime, "_operational_day")
    FakeModel(monkeypatch, cost=Fraction(3, 100))
    first = unit_session(tmp_path, monkeypatch, **{live_config.BUDGET_USD: "1/2"})
    call(first.client(Owner(), context_guard=None), "one")
    assert first.snapshot()["remaining"]["USD"] == "47/100"
    # Any later session on the same directory sees the same total: nothing resets it.
    later = unit_session(tmp_path, monkeypatch, **{live_config.BUDGET_USD: "1/2"})
    assert later.snapshot()["remaining"]["USD"] == "47/100"
    # Only a new journal directory (or a higher cap) starts a new total.
    fresh = LiveSession(
        LiveConfig.from_environ(
            environ(
                tmp_path,
                **{
                    live_config.JOURNAL_DIR: str(tmp_path / "journal-2"),
                    live_config.BUDGET_USD: "1/2",
                },
            )
        )
    )
    assert fresh.authorize(ACCESS_CODE) == "accepted"
    assert fresh.snapshot()["remaining"]["USD"] == "1/2"
    with contextlib.closing(sqlite3.connect(tmp_path / "journal" / "live-ledger.sqlite3")) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(spend)")}
    assert "day" not in columns and {"currency", "deployment", "amount", "settled"} <= columns


# --- M4: the judge strip --------------------------------------------------------------------


@pytest.mark.parametrize("locale", [ZH, "en"])
def test_the_judge_strip_names_the_live_planner(monkeypatch, tmp_path, locale):
    FakeModel(monkeypatch)
    page = start(monkeypatch, tmp_path, locale=locale)
    scripted_strip = msg("polish.view.judge_strip", locale)
    live_strip = msg("live.judge_strip", locale, deployment=LABEL)
    assert scripted_strip in shown_text(page) and live_strip not in shown_text(page)
    unlock(page)
    switch(page, True)
    controller = page.session_state["controller"]
    pages = [option for option in page.selectbox(key="page").options]
    assert pages
    for route in ("conversation", "transfers", "wealth", "device", "audit", "im"):
        navigate(page, route)
        assert not page.exception, route
        text = shown_text(page)
        assert scripted_strip not in text, route
        assert live_strip in text, route
        assert controller.client.mode == "record"
    switch(page, False)
    text = shown_text(page)
    assert scripted_strip in text and live_strip not in text


# --- M5: the wealth page --------------------------------------------------------------------


def test_the_wealth_page_badge_describes_its_scripted_advisor(monkeypatch, tmp_path):
    FakeModel(monkeypatch)
    page = start(monkeypatch, tmp_path)
    unlock(page)
    switch(page, True)
    navigate(page, "wealth")
    assert not page.exception
    assert msg("live.badge.scripted") in badge(page)[-1]
    assert not any("live-badge-model" in item for item in badge(page))
    text = shown_text(page)
    assert msg("live.scope.wealth", deployment=LABEL) in text
    # The page's own scripted caption now agrees with its badge.
    assert msg("polish.wealth.evidence_mode") in text
    assert page.session_state["wealth_controller"].client.mode == "scripted"
    navigate(page, "conversation")
    assert msg("live.badge.live", deployment=LABEL) in badge(page)[-1]
    # An investment request: classified live, its goal handled by the scripted advisor.
    send(page, "有五万闲钱放哪好")
    reply = response(page)
    assert reply["intent"] == "investment" and reply["planner_mode"] == "record"
    assert msg("live.note.wealth_scripted") in shown_text(page)
    assert msg("live.badge.live", deployment=LABEL) in badge(page)[-1]


# --- M6: judge counts after the return to scripted -------------------------------------------


def test_judge_counts_and_journals_survive_the_return_to_scripted(monkeypatch, tmp_path):
    FakeModel(monkeypatch)
    page = start(monkeypatch, tmp_path)
    unlock(page)
    switch(page, True)
    live = page.session_state["live_session"]
    send(page, "查一下余额")
    first = f"{DEPLOYMENTS[0].id}/session-0001"
    assert live.snapshot()["journal_dir"] == first
    switch(page, False)
    info = live.snapshot()
    assert info["journal_dir"] is None and info["journal_dirs"] == [first]
    assert info["calls"] == 1 and info["api_calls"] == 1
    text = shown_text(page)
    assert "（实际请求 1 次）" in text and first in text
    switch(page, True)
    send(page, "查一下余额")
    second = f"{DEPLOYMENTS[0].id}/session-0002"
    info = live.snapshot()
    assert info["journal_dirs"] == [first, second] and info["api_calls"] == 2
    text = shown_text(page)
    assert first in text and second in text and "（实际请求 2 次）" in text


def test_judge_counts_survive_an_automatic_fallback(monkeypatch, tmp_path):
    FakeModel(monkeypatch)
    page = start(monkeypatch, tmp_path, **{live_config.SESSION_CALLS: "1"})
    unlock(page)
    switch(page, True)
    send(page, "查一下余额")
    controller = page.session_state["controller"]
    assert controller.client.mode == "scripted"
    text = shown_text(page)
    assert msg("live.exhausted_notice", reason=msg("live.exhausted.calls")) in text
    assert "本会话实时调用 1 次（实际请求 1 次）" in text
    assert f"{DEPLOYMENTS[0].id}/session-0001" in text


def test_counts_are_current_on_the_run_where_a_live_request_failed(monkeypatch, tmp_path):
    FakeModel(monkeypatch, behaviour="bad")
    page = start(monkeypatch, tmp_path)
    unlock(page)
    switch(page, True)
    send(page, "给妈妈转800元")
    text = shown_text(page)
    assert failure_line("output-rejected") in text
    assert "本会话实时调用 1 次（实际请求 1 次）" in text
    assert "本会话实时调用 0 次" not in text


# --- M7: per-request disclosure and real timeouts ------------------------------------------


def test_a_partly_scripted_reply_is_disclosed_for_the_whole_request(monkeypatch, tmp_path):
    fake = FakeModel(monkeypatch, plan=["timeout", "ok"])
    page = start(monkeypatch, tmp_path)
    unlock(page)
    switch(page, True)
    controller = page.session_state["controller"]
    send(page, "给妈妈转800元")
    assert [c["source"] for c in controller.client.calls] == ["scripted-timeout", "live-recorded"]
    assert fake.attempts == 2
    reply = response(page)
    assert reply["planner_mode"] == "record" and reply["card"]
    note = msg("live.note.partly_scripted", count=1)
    assert note in shown_text(page)
    assert msg("live.note.calls", calls=2, recorded=1, timeouts=1, rejected=0) in shown_text(page)
    # The note stays with that reply, and only with it.
    page.run()
    assert note in shown_text(page)
    page.button(key="domestic_cancel").click().run()
    send(page, "查一下余额")
    assert note not in shown_text(page)


def test_a_transport_timeout_is_a_failure_for_every_registered_deployment(monkeypatch, tmp_path):
    files = sorted({item.env_file for item in DEPLOYMENTS})
    config = LiveConfig.from_environ({
        live_config.SWITCH: "1",
        live_config.ENV_DIR: str(credential_dir(tmp_path, files=files)),
        live_config.JOURNAL_DIR: str(tmp_path / "journal"),
    })

    def timed_out(self, request):
        raise httpx.ReadTimeout("test: simulated provider timeout", request=request)

    with no_network(monkeypatch):
        monkeypatch.setattr(httpx.HTTPTransport, "handle_request", timed_out)
        for item in DEPLOYMENTS:
            session = LiveSession(config)
            session.select(item.id)
            assert session.authorize(ACCESS_CODE) == "accepted", item.id
            client = session.client(Owner(), context_guard=None)
            with pytest.raises(LLMOutputError):
                client.complete(
                    task_id="S7R",
                    step="timeout",
                    prompt={
                        "instruction": "classify",
                        "user_text": "balance",
                        "response_schema": {
                            "type": "object",
                            "properties": {"intent": {"type": "string"}},
                            "required": ["intent"],
                            "additionalProperties": False,
                        },
                    },
                    tool_state={},
                    scripted=lambda: {"intent": "balance_query"},
                )
            assert client.calls[-1]["source"] == "live-error", item.id
            assert session.last_call() == {"source": "live-error", "failed": True}
            assert journal_records(client.replay_dir) == [], item.id
            assert session.flush()
            index = (client.replay_dir / live_runtime.OUTCOMES).read_text(encoding="utf-8")
            assert [json.loads(line)["source"] for line in index.splitlines()] == ["live-error"]


def test_a_provider_error_is_named_plainly_and_only_judges_see_its_code(monkeypatch, tmp_path):
    FakeModel(monkeypatch, behaviour="error")
    page = start(monkeypatch, tmp_path)
    unlock(page)
    switch(page, True)
    send(page, "给妈妈转800元")
    assert failure_line("live-error") in shown_text(page)
    customer = start(monkeypatch, tmp_path / "customer", view="customer")
    unlock(customer)
    switch(customer, True)
    send(customer, "给妈妈转800元")
    text = shown_text(customer)
    plain = msg("live.customer.failure", reason=msg("live.reason_of.live-error"))
    assert plain in text and "live-error" not in text


def test_an_output_refused_by_the_agent_is_named_and_indexed(monkeypatch, tmp_path):
    FakeModel(monkeypatch, rule=bad_intent_schema)
    page = start(monkeypatch, tmp_path)
    unlock(page)
    switch(page, True)
    controller = page.session_state["controller"]
    send(page, "给妈妈转800元")
    text = shown_text(page)
    assert GENERIC in text and failure_line("agent-rejected") in text
    assert [c["source"] for c in controller.client.calls] == ["live-recorded"]
    index = (controller.client.replay_dir / live_runtime.OUTCOMES).read_text(encoding="utf-8")
    rows = [json.loads(line) for line in index.splitlines()]
    assert rows == [
        {
            "agent_rejected": True,
            "failed": False,
            "request_hash": controller.client.calls[0]["request_hash"],
            # External review 3: each line also says how the attempt's cost was settled.
            "settlement": "settled",
            "source": "live-recorded",
            "step": controller.client.calls[0]["step"],
        }
    ]
    # A later failure that reaches no model is not attributed to it.
    send_long = "转" * 4001
    page.text_input(key="domestic_text").set_value(send_long)
    page.button(key="domestic_send").click().run()
    assert not page.exception
    text = shown_text(page)
    assert GENERIC in text
    assert msg("live.failure").split("{")[0] not in text


# --- M8: process environment variables ------------------------------------------------------


def test_blank_or_off_live_variables_never_break_the_page(monkeypatch, tmp_path):
    blank = {name: "" for name in live_config.PROCESS_NAMES}
    config = LiveConfig.from_environ({**blank, live_config.SWITCH: "1"})
    assert config.enabled and config.session_calls == live_config.DEFAULT_SESSION_CALLS
    assert config.budget_cap("USD") == live_config.DEFAULT_BUDGET_USD
    assert config.journal_dir is None and config.env_dir is None
    off = LiveConfig.from_environ({
        live_config.SESSION_CALLS: "two",
        live_config.BUDGET_USD: "-1",
        live_config.BUDGET_CNY: "nan",
    })
    assert off.enabled is False
    with pytest.raises(LLMConfigurationError):
        LiveConfig.from_environ({live_config.SWITCH: "1", live_config.BUDGET_USD: "-1"})
    # A template exported as-is (every name blank) leaves the scripted page unchanged.
    for name in live_config.PROCESS_NAMES:
        monkeypatch.setenv(name, "")
    monkeypatch.setenv("CORRIDOR_LOCALE", ZH)
    monkeypatch.setenv("CORRIDOR_DEFAULT_VIEW", "customer")
    page = app()
    assert not page.exception
    assert msg("live.config_invalid") not in shown_text(page)
    assert not [item for item in page.toggle if item.key == "live_mode"]


def test_the_env_template_names_live_variables_only_as_comments():
    lines = [line.strip() for line in (ROOT / ".env.example").read_text("utf-8").splitlines()]
    for name in live_config.PROCESS_NAMES:
        assert not [line for line in lines if line.startswith(name + "=")], name
        assert [line for line in lines if line.startswith(f"# {name}=")], name
    assert not [line for line in lines if "LIVE_DAILY_" in line]
    text = "\n".join(lines)
    assert "PROCESS ENVIRONMENT" in text and "$env:CORRIDOR_LIVE='1'" in text


# --- adopted suggestions --------------------------------------------------------------------


def test_wrong_access_codes_lock_live_access_across_sessions(tmp_path):
    attempts = 0
    while attempts < live_runtime.GLOBAL_LOCK_AFTER:
        session = LiveSession(LiveConfig.from_environ(environ(tmp_path)))
        for _ in range(
            min(live_runtime.LOCK_AFTER_FAILURES, live_runtime.GLOBAL_LOCK_AFTER - attempts)
        ):
            assert session.authorize("nope") == "invalid"
            attempts += 1
    fresh = LiveSession(LiveConfig.from_environ(environ(tmp_path)))
    assert fresh.snapshot()["reason"] == "locked_all"
    assert fresh.authorize(ACCESS_CODE) == "locked_all"
    assert fresh.snapshot()["authorized"] is False
    # Nothing about a code is stored: only the bare count of failures.
    ledger = (tmp_path / "journal" / "live-ledger.sqlite3").read_bytes()
    assert b"nope" not in ledger and ACCESS_CODE.encode("utf-8") not in ledger
    # Another journal directory is another ledger.
    other = LiveSession(
        LiveConfig.from_environ(
            environ(tmp_path, **{live_config.JOURNAL_DIR: str(tmp_path / "j2")})
        )
    )
    assert other.authorize(ACCESS_CODE) == "accepted"


def test_a_locked_session_keeps_saying_so_and_disables_unlock(monkeypatch, tmp_path):
    page = start(monkeypatch, tmp_path)
    for _ in range(live_runtime.LOCK_AFTER_FAILURES):
        unlock(page, "wrong-code")
    page.run()
    assert msg("live.reason.locked") in shown_text(page)
    assert access_field_disabled() is True
    assert "新开" not in msg("live.locked_out") and "new session" not in msg(
        "live.locked_out", "en"
    )


@pytest.mark.parametrize("name", ["LLM_REASONING_EFFORT", "LLM_MAX_USD", "LLM_API_KEY"])
def test_inherited_llm_settings_make_live_access_unavailable(tmp_path, monkeypatch, name):
    fake = FakeModel(monkeypatch)
    session = unit_session(tmp_path, monkeypatch)
    monkeypatch.setenv(name, "high-secretish-value")
    info = session.snapshot()
    assert info["reason"] == "inherited_env" and info["inherited_env"] == [name]
    assert "high-secretish-value" not in json.dumps(info, default=str)
    with pytest.raises(BudgetExceeded):
        session.client(Owner(), context_guard=None)
    assert LiveSession(LiveConfig.from_environ(environ(tmp_path))).authorize(ACCESS_CODE) == (
        "unavailable"
    )
    assert fake.attempts == 0


def test_live_mode_requires_an_explicit_journal_directory(tmp_path, monkeypatch):
    creds = credential_dir(tmp_path)
    session = LiveSession(
        LiveConfig.from_environ({live_config.SWITCH: "1", live_config.ENV_DIR: str(creds)})
    )
    assert session.snapshot()["reason"] == "missing_journal"
    assert session.authorize(ACCESS_CODE) == "unavailable"
    with pytest.raises(BudgetExceeded):
        session.client(Owner(), context_guard=None)


def test_each_journal_directory_states_its_currency_and_indexes_outcomes(tmp_path, monkeypatch):
    from corridor.agent.llm import record_model_output_rejection

    FakeModel(monkeypatch)
    creds = credential_dir(tmp_path, files=(".env.deepseek",))
    session = LiveSession(
        LiveConfig.from_environ({
            live_config.SWITCH: "1",
            live_config.ENV_DIR: str(creds),
            live_config.JOURNAL_DIR: str(tmp_path / "journal"),
        })
    )
    session.select("deepseek-flash")
    assert session.authorize(ACCESS_CODE) == "accepted"
    client = session.client(Owner(), context_guard=None)
    manifest = json.loads((client.replay_dir / live_runtime.MANIFEST).read_text("utf-8"))
    assert manifest["deployment"] == "deepseek-flash" and manifest["currency"] == "CNY"
    assert "CNY" in manifest["cost_field_note"] and "cost_usd" in manifest["cost_field_note"]
    assert (manifest["assumed_price_in"], manifest["assumed_price_out"]) == ("2", "8")
    call(client, "one")
    call(client, "two")
    # The agent refuses the second recorded output after the client accepted it.
    record_model_output_rejection(client, ValueError("refused"), {"intent": "bogus"})
    assert session.flush()
    rows = [
        json.loads(line)
        for line in (client.replay_dir / live_runtime.OUTCOMES).read_text("utf-8").splitlines()
    ]
    assert [(row["source"], row["agent_rejected"]) for row in rows] == [
        ("live-recorded", False),
        ("live-recorded", True),
    ]
    for path in client.replay_dir.rglob("*"):
        if path.is_file():
            assert FAKE_KEY.encode("utf-8") not in path.read_bytes(), path
            assert ACCESS_CODE.encode("utf-8") not in path.read_bytes(), path


def test_a_cap_reached_during_a_transaction_says_what_happens_next(monkeypatch, tmp_path):
    FakeModel(monkeypatch)
    page = start(monkeypatch, tmp_path, **{live_config.SESSION_CALLS: "2"})
    unlock(page)
    switch(page, True)
    controller = page.session_state["controller"]
    send(page, "给妈妈转5000元")
    assert response(page)["card"]["tier"] == "red"
    live = page.session_state["live_session"]
    assert live.snapshot()["calls"] == 2 and live.snapshot()["reason"] == "budget_exhausted"
    assert controller.at_transaction_boundary() is False
    text = shown_text(page)
    assert msg("live.reason.budget_pending") in text
    assert msg("live.reason.budget_exhausted") not in text
    assert msg("live.badge.live_exhausted", deployment=LABEL) in badge(page)[-1]
    page.button(key="domestic_cancel").click().run()
    assert not page.exception
    assert controller.client.mode == "scripted"
    assert msg("live.exhausted_notice", reason=msg("live.exhausted.calls")) in shown_text(page)


def test_customer_view_live_controls_use_plain_copy(monkeypatch, tmp_path):
    page = start(monkeypatch, tmp_path, view="customer")
    text = shown_text(page)
    assert msg("live.customer.intro") in text
    for jargon in (
        "CORRIDOR_LIVE",
        "LIVE_JOURNAL_DIR",
        "凭据文件",
        "每百万 token",
        "docs/MODELS.md",
        "paid-v3",
        "冻结内核",
        "登记价",
    ):
        assert jargon not in text, jargon
    options = page.selectbox(key="live_deployment").options
    assert options and not [option for option in options if "凭据" in option]


def corner(page):
    return [str(item.value) for item in page.markdown if "simulation-persistent" in str(item.value)]


def test_the_expander_and_the_demo_preset_say_which_planner_runs(monkeypatch, tmp_path):
    FakeModel(monkeypatch)
    page = start(monkeypatch, tmp_path)
    notice = UI_MESSAGES["simulation_notice"][ZH]
    assert len(corner(page)) == 1 and msg("live.corner", deployment=LABEL) not in corner(page)[0]
    unlock(page)
    switch(page, True)
    assert msg("live.reason.on", deployment=LABEL) in shown_text(page)
    assert msg("live.reason.ready") not in shown_text(page)
    # The always-visible corner notice names the live planner as well.
    assert corner(page) == [corner(page)[0]]
    assert f"{notice} · {msg('live.corner', deployment=LABEL)}" in corner(page)[0]
    page.button(key="load_demo_preset").click().run()
    assert not page.exception and page.session_state["show_demo"] is True
    assert msg("live.badge.scripted") in badge(page)[-1]
    assert msg("live.scope.preset", deployment=LABEL) in shown_text(page)
    page.button(key="close_demo").click().run()
    assert not page.exception
    assert msg("live.scope.preset", deployment=LABEL) not in shown_text(page)
    switch(page, False)
    assert msg("live.corner", deployment=LABEL) not in corner(page)[0]
    assert notice in corner(page)[0]


def test_a_clarify_reply_from_the_live_planner_is_explained_to_judges(monkeypatch, tmp_path):
    FakeModel(monkeypatch)
    page = start(monkeypatch, tmp_path)
    unlock(page)
    switch(page, True)
    send(page, "给小王转800元")
    assert response(page)["status"] == "clarify"
    assert msg("live.note.clarify") in shown_text(page)
    page.button(key="view_toggle").click().run()
    assert msg("live.note.clarify") not in shown_text(page)
