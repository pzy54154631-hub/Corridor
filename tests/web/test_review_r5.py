"""Regression tests for the fifth review round (R5) of the web front end.

Each test names the item it closes:

- ``test_i1_*`` (item 1, safety): a call that changed state and then failed with an unknown
  result, while a closing step (the live sync, the refresh) raises a refusal or a contained
  result as well. The unknown result stands, the session is distrusted, and no token held
  before approves, signs, sends or submits a code, or dispatches. The same for the other
  closing paths of ``Write.run`` and ``Write.perform``: after a successful call, after an
  expected failure, and with a failure log that fails itself.
- ``test_i2_*`` (item 2, the same class): any exception from inside a chat choice's or the
  AA preparer, a ``WebError`` of any status included, is an unknown result; a refusal at
  the operation's entry, before the preparer runs, keeps its status. And the sweep of the
  web layer's other state-changing product calls: every ``live.operation``, the delivery
  of a requested code, the recipient check's answer and retry, the recording of a reply.
- ``test_i3_*`` (item 3): E-15's check fails with a lock that does not exclude; E-14's
  counters each count a positive calibration.
- ``test_i5_*`` (item 5, the wording): the two answers below routing that a closed lab
  path can get instead of its 404, tornado's own 400 and 405, touch nothing.

The real handlers, service functions, controllers, gateway and kernel run throughout,
except where a test injects the fault it names. This module runs the
submission build (``FEATURES`` as shipped); the AA tests open ``aa`` explicitly.
"""

from collections import Counter
from contextlib import contextmanager

import pytest

import corridor.web as web_package
from corridor.agent.domestic_session import DomesticSession
from corridor.ui.controller import DemoController
from corridor.web import compat, errors, live, logs, service, tasks
from corridor.web import refresh as refresh_module
from corridor.web import state as web_state
from corridor.web.executor import bound
from tests.unit.test_ui_scam_friction import CLARIFY_PLAIN, NEW, SAFE
from tests.web.conftest import serve
from tests.web.test_api_chat import Browser
from tests.web.test_review_r1 import finish, held_body, newest, spied
from tests.web.test_review_r2 import raw_request
from tests.web.test_review_r3 import (
    COUNTED,
    LAB_POSTS,
    Walk,
    dispatched_plan,
    open_query,
    schedule_listing,
)
from tests.web.test_review_r4 import (
    JOB,
    LAB_GET,
    LAB_OTHER_PATHS,
    READ_COUNTED,
    WEB_ERRORS,
    ReadWalk,
    RecordedLock,
    assert_one_lock_serializes,
    traced,
    two_pool_record,
)
from tests.web.test_tasks import RED, YELLOW, logged_in

# The device's and the gateway's entry points that an old token must never reach again.
COUNTERS = ("approve", "confirm_input", "complete_mfa", "request_mfa", "submit_mfa", "dispatch")
AFTER = {"text": YELLOW, "external_files": 0, "client_msg_id": "r5-after-0001"}


@pytest.fixture
def browser(web_app):
    page = Browser(web_app)
    page.login()
    return page


def held(page):
    """Every token the page holds now, as the body a gesture would send: {tkey: body}."""
    return {
        task["key"]: {
            "gesture": task["gesture"]["token"],
            "card_version": task["gesture"]["card_version"],
        }
        for task in page.state["device"]["tasks"]
        if task["gesture"] is not None
    }


def assert_distrusted(page, data, saved, monkeypatch, *, code=None, error="outcome_unknown"):
    """The answer carries the call's own result (``error``) and the state; the session is
    distrusted and every token and gate record is gone. No token held before the failure
    approves, signs an input, sends a code, submits a code (the real one, where the test
    read it) or dispatches, and no new request is accepted."""
    assert data["ok"] is False and data["error"]["code"] == error, data
    assert page.ws.untrusted is True
    assert page.ws.gestures == {} and page.ws.gate_eval == {}
    assert all(task["gesture"] is None for task in data["state"]["device"]["tasks"])
    counts = spied(monkeypatch, *COUNTERS)
    for tkey, body in saved.items():
        for path, extra in (
            ("confirm", {}),
            ("mfa/request", {}),
            ("mfa/verify", {"code": code or "000000"}),
        ):
            refused = page.post(f"/api/device/tasks/{tkey}/{path}", {**body, **extra}, expect=409)
            assert refused["ok"] is False, refused
            assert refused["error"]["code"] == "not_applicable", refused
    # The HTTP status (409) is asserted by ``post`` itself, the error code here (RW-6 item 4).
    refused = page.post("/api/chat/message", AFTER, expect=409)
    assert refused["ok"] is False and refused["error"]["code"] == "not_applicable", refused
    assert counts == Counter()
    assert page.ws.untrusted is True and page.ws.gestures == {}


# --- RW-4'''' item 1: a closing failure never replaces the call's own result ----------------


class Closing:
    """A closing step, the refresh or the live sync, that raises once armed. The fault that
    makes the call fail arms it, so every step before the failure ran as usual."""

    def __init__(self, monkeypatch, step, raised):
        owner, name = (refresh_module, "refresh") if step == "refresh" else (live, "sync")
        original = getattr(owner, name)
        self.armed = False
        self.raised = 0

        def closing(*args, **kwargs):
            if self.armed:
                self.raised += 1
                raise raised()
            return original(*args, **kwargs)

        monkeypatch.setattr(owner, name, closing)

    def arm(self):
        self.armed = True

    def disarm(self):
        self.armed = False
        assert self.raised >= 1, "the closing step was reached and raised"


# What a closing step raises: a refusal of each status class, or a contained result.
CLOSING = {
    "refresh-404": ("refresh", lambda: errors.error("not_found")),
    "refresh-409": ("refresh", lambda: errors.error("not_applicable")),
    "refresh-contained": ("refresh", errors.WebChildOutcomeUnknown),
    "sync-400": ("sync", lambda: errors.error("bad_json")),
    "sync-422": ("sync", lambda: errors.error("invalid_input")),
    "sync-contained": ("sync", errors.WebChildOutcomeUnknown),
}


def birthday_unknown(page, monkeypatch, closing):
    """choose_birthday: the preparer records the request and registers the plan, then
    raises a refusal; the service answers that as an unknown result."""
    open_query(page)
    rkey = page.send("帮我准备生日")
    choice = next(
        item["key"] for item in page.response(rkey)["birthday"]["choices"] if item["enabled"]
    )
    c = page.ws.controller
    plans, claimed = len(c._plans), len(c._request_facts)
    original = DemoController.preview_birthday

    def prepared_then_raised(self, *args, **kwargs):
        original(self, *args, **kwargs)
        closing.arm()
        raise errors.error("not_applicable")

    monkeypatch.setattr(DemoController, "preview_birthday", prepared_then_raised)
    saved = held(page)
    data = page.post(f"/api/chat/responses/{rkey}/birthday", {"choice": choice}, expect=500)
    assert len(c._plans) == plans + 1 and len(c._request_facts) == claimed + 1
    return data, saved, None


def compensate_unknown(page, monkeypatch, closing):
    """compensate_latest: the gateway audits the request and the controller prepares the
    unlock card, then a refusal is raised; the service answers that as unknown."""
    dispatched_plan(page)
    open_query(page)
    saved = held(page)
    c = page.ws.controller
    pending = set(c._pending)
    original = DemoController.compensate

    def prepared_then_raised(self, plan_id):
        original(self, plan_id)
        closing.arm()
        raise errors.error("not_applicable")

    monkeypatch.setattr(DemoController, "compensate", prepared_then_raised)
    version = page.tab("operator")["compensation"]["compensate_version"]
    data = page.post("/api/chat/compensate", {"plan_version": version}, expect=500)
    (prepared,) = set(c._pending) - pending
    assert c._pending[prepared]["compensation_plan_id"]
    return data, saved, None


def confirm_unknown(page, monkeypatch, closing):
    """An ordinary business gesture: the device confirms a transfer and the bank executes
    it, then the call raises (the two-phase commit makes that an unknown result)."""
    open_query(page)
    tkey = "T" + page.send(YELLOW)
    saved = held(page)
    bank = page.ws.controller.gateway.bank
    executed = bank.execute_count
    original = service._confirm_call

    def dispatched_then_raised(ws, task, info):
        original(ws, task, info)
        closing.arm()
        raise TypeError("after the dispatch")

    monkeypatch.setattr(service, "_confirm_call", dispatched_then_raised)
    data = page.post(f"/api/device/tasks/{tkey}/confirm", saved[tkey], expect=500)
    assert bank.execute_count == executed + 1
    return data, saved, None


def query_unknown(page, monkeypatch, closing):
    """An ordinary business page action: the controller prepares the query's card, then
    the call raises an unknown result. A red payment waits for its code meanwhile, so the
    held tokens include one that would submit the real code."""
    red = "T" + page.send(RED)
    page.device(red, "confirm")
    page.device(red, "mfa_request")
    code = page.sms_code()
    saved = held(page)
    assert {page.task(tkey)["gesture"]["op"] for tkey in saved} == {"mfa_verify"}
    c = page.ws.controller
    pending = set(c._pending)
    original = DemoController.preview_subscriptions

    def prepared_then_unknown(self, *args, **kwargs):
        original(self, *args, **kwargs)
        closing.arm()
        raise errors.WebOutcomeUnknown()

    monkeypatch.setattr(DemoController, "preview_subscriptions", prepared_then_unknown)
    data = page.post("/api/subscriptions/query", {}, expect=500)
    assert set(c._pending) - pending
    return data, saved, code


UNKNOWN_CALLS = {
    "birthday": birthday_unknown,
    "compensate": compensate_unknown,
    "device-confirm": confirm_unknown,
    "subscriptions-query": query_unknown,
}


@pytest.mark.parametrize("fault", list(CLOSING))
@pytest.mark.parametrize("call", list(UNKNOWN_CALLS))
def test_i1_a_closing_failure_never_replaces_an_unknown_result(browser, monkeypatch, call, fault):
    """RW-4'''' item 1: state changed, the call failed with an unknown result, and the live
    sync or the refresh that closes it raised a refusal or a contained result too. The
    answer is still ``outcome_unknown`` and the session is distrusted."""
    closing = Closing(monkeypatch, *CLOSING[fault])
    data, saved, code = UNKNOWN_CALLS[call](browser, monkeypatch, closing)
    closing.disarm()
    assert saved
    assert_distrusted(browser, data, saved, monkeypatch, code=code)


# The closing faults after a call that ran (a refusal or a contained result, each step).
RAN_CLOSING = ["refresh-409", "refresh-contained", "sync-422", "sync-contained"]


def confirm_succeeds(page, monkeypatch, closing):
    """The device confirms a transfer and the bank executes it; the call returns."""
    open_query(page)
    tkey = "T" + page.send(YELLOW)
    saved = held(page)
    bank = page.ws.controller.gateway.bank
    executed = bank.execute_count
    original = service.device_confirm

    def confirmed(*args, **kwargs):
        result = original(*args, **kwargs)
        closing.arm()
        return result

    monkeypatch.setattr(service, "device_confirm", confirmed)
    data = page.post(f"/api/device/tasks/{tkey}/confirm", saved[tkey], expect=500)
    assert bank.execute_count == executed + 1
    return data, saved, None


def query_succeeds(page, monkeypatch, closing):
    """A business page action prepares a card; the call returns."""
    page.send(YELLOW)
    saved = held(page)
    pending = set(page.ws.controller._pending)
    original = service.subscriptions_query

    def queried(ws):
        original(ws)
        closing.arm()

    monkeypatch.setattr(service, "subscriptions_query", queried)
    data = page.post("/api/subscriptions/query", {}, expect=500)
    assert set(page.ws.controller._pending) - pending
    return data, saved, None


@pytest.mark.parametrize("fault", RAN_CLOSING)
@pytest.mark.parametrize("call", ["device-confirm", "subscriptions-query"])
def test_i1_a_closing_refusal_after_a_call_that_ran_is_an_unknown_result(
    browser, monkeypatch, call, fault
):
    """The other closing path of ``Write.run``: the call ran and changed state, then the
    closing sync or refresh raised a refusal or a contained result. That would say nothing
    changed; the answer is ``outcome_unknown`` and the session is distrusted."""
    closing = Closing(monkeypatch, *CLOSING[fault])
    scenario = confirm_succeeds if call == "device-confirm" else query_succeeds
    data, saved, _ = scenario(browser, monkeypatch, closing)
    closing.disarm()
    assert saved
    assert_distrusted(browser, data, saved, monkeypatch)


# Besides the refusals: a closing step that fails in an unexpected way.
EXPECTED_CLOSING = {
    "refresh-409": CLOSING["refresh-409"],
    "refresh-contained": CLOSING["refresh-contained"],
    "sync-422": CLOSING["sync-422"],
    "refresh-TypeError": ("refresh", lambda: TypeError("the refresh broke")),
}


@pytest.mark.parametrize("fault", list(EXPECTED_CLOSING))
def test_i1_a_closing_failure_after_an_expected_failure_distrusts_the_session(
    browser, monkeypatch, fault
):
    """An expected failure (``operation_failed``, the device call raised before anything
    was signed) is answered as it is, never replaced by the closing step's error; a closing
    step that fails after it distrusts the session, as any unexpected failure does (before
    RW-4'''' an unexpected closing error replaced the answer and distrusted it: still so)."""
    closing = Closing(monkeypatch, *EXPECTED_CLOSING[fault])
    open_query(browser)
    tkey = "T" + browser.send(YELLOW)
    saved = held(browser)
    bank = browser.ws.controller.gateway.bank
    executed = bank.execute_count

    def failed(ws, task, info):
        closing.arm()
        raise RuntimeError("an expected failure")

    monkeypatch.setattr(service, "_confirm_call", failed)
    data = browser.post(f"/api/device/tasks/{tkey}/confirm", saved[tkey], expect=422)
    closing.disarm()
    assert bank.execute_count == executed
    assert_distrusted(browser, data, saved, monkeypatch, error="operation_failed")


def test_i1_a_failure_log_that_fails_itself_changes_nothing(browser, monkeypatch):
    """The closing failure, the distrust and the unknown result do not depend on the log:
    a failure log that raises is ignored."""

    def broken(*args, **kwargs):
        raise RuntimeError("the failure log broke")

    monkeypatch.setattr(logs, "failure", broken)
    closing = Closing(monkeypatch, *CLOSING["refresh-409"])
    data, saved, code = birthday_unknown(browser, monkeypatch, closing)
    closing.disarm()
    assert_distrusted(browser, data, saved, monkeypatch, code=code)


# --- RW-4'''' item 2: the chat choice and the AA preparers ----------------------------------

# Whatever the preparer raises after it changed state: every WebError class of R4, an
# expected failure of the live operation's set and any other exception.
RAISED = {
    **WEB_ERRORS,
    "RuntimeError": lambda: RuntimeError("inside the preparer"),
    "TypeError": lambda: TypeError("inside the preparer"),
}


def subscription_choice(page):
    rkey = page.send("取消订阅")
    version = page.response(rkey)["subscription_select"]["list_version"]
    return f"/api/chat/responses/{rkey}/subscription", {"choice": "c0", "list_version": version}


def schedule_choice(page):
    listed, shown = schedule_listing(page)
    body = {"choice": "c0", "list_version": shown["list_version"]}
    return f"/api/chat/responses/{listed}/schedule-cancel", body


CHOICES = {
    "subscription": (subscription_choice, "prepare_subscription_cancel"),
    "schedule-cancel": (schedule_choice, "prepare_schedule_cancel"),
}


@pytest.mark.parametrize("raised", list(RAISED.values()), ids=list(RAISED))
@pytest.mark.parametrize("choice", list(CHOICES))
def test_i2_any_exception_from_a_chat_choice_preparer_is_an_unknown_result(
    browser, monkeypatch, choice, raised
):
    """``service._prepare_choice``: once ``live.operation`` is entered, anything the
    preparer raises, a ``WebError`` of any status included, follows a prepared card: an
    unknown result, the session distrusted."""
    open_query(browser)
    setup, name = CHOICES[choice]
    path, body = setup(browser)
    saved = held(browser)
    c = browser.ws.controller
    pending = set(c._pending)
    original = getattr(DomesticSession, name)
    ran = []

    def prepared_then_raised(self, *args, **kwargs):
        ran.append(original(self, *args, **kwargs))
        raise raised()

    monkeypatch.setattr(DomesticSession, name, prepared_then_raised)
    data = browser.post(path, body, expect=500)
    # The preparer did prepare its card before the error.
    assert len(ran) == 1 and set(c._pending) - pending
    assert saved
    assert_distrusted(browser, data, saved, monkeypatch)


def test_i2_a_refusal_at_the_operations_entry_keeps_its_status(browser, monkeypatch):
    """A refusal raised where ``live.operation`` is entered, before ``prepare()`` is
    called, changed nothing: it keeps its own status, and the session stays trusted."""
    tkey, saved = open_query(browser)
    path, body = subscription_choice(browser)
    called = []
    monkeypatch.setattr(
        DomesticSession, "prepare_subscription_cancel", lambda self, *a, **k: called.append(a)
    )

    @contextmanager
    def refused_on_entry(ws, prefix, rid=None, *, bind=True):
        raise errors.error("live_unavailable")
        yield  # the entry refuses first

    operation = live.operation
    monkeypatch.setattr(live, "operation", refused_on_entry)
    data = browser.post(path, body, expect=409)
    assert data["error"]["code"] == "live_unavailable" and called == []
    assert not browser.ws.untrusted
    monkeypatch.setattr(live, "operation", operation)
    # The session still works: the business card's token is honoured.
    result = browser.post(f"/api/device/tasks/{tkey}/confirm", saved)["result"]
    assert result["status"] == "settled" and not browser.ws.untrusted


@pytest.fixture
def aa_page():
    # AA (P1) is closed in the submission build; this test opens it explicitly.
    with serve({"features": web_package.FEATURES | {"aa"}}) as app:
        page = Browser(app)
        page.login()
        yield page


@pytest.mark.parametrize("raised", list(RAISED.values()), ids=list(RAISED))
def test_i2_any_exception_from_the_aa_preparer_is_an_unknown_result(aa_page, monkeypatch, raised):
    """``complete_aa``: ``prepare_aa`` prepared the new reply, then raised: an unknown
    result whatever it raised (before RW-4'''' only three exception types were)."""
    open_query(aa_page)
    rkey = aa_page.send("这顿饭460我们四个AA")
    saved = held(aa_page)
    turns = len(aa_page.ws.transcript)
    original = DomesticSession.prepare_aa
    ran = []

    def prepared_then_raised(self, *args, **kwargs):
        ran.append(original(self, *args, **kwargs))
        raise raised()

    monkeypatch.setattr(DomesticSession, "prepare_aa", prepared_then_raised)
    path = f"/api/chat/responses/{rkey}/aa"
    data = aa_page.post(path, {"choices": ["c0", "c1", "c2"]}, expect=500)
    assert len(ran) == 1 and ran[0]["response_id"]
    assert len(aa_page.ws.transcript) == turns
    assert saved
    assert_distrusted(aa_page, data, saved, monkeypatch)


# --- RW-4'''' item 2: the sweep of the other state-changing product calls -------------------


def op_login(page):
    return "/api/session/login", {}


def op_message(page):
    return "/api/chat/message", {"text": YELLOW, "external_files": 0, "client_msg_id": "r5-op-0001"}


def op_shortcut(page):
    return "/api/chat/shortcut", {"shortcut": "transfer", "client_msg_id": "r5-op-0001"}


def op_recipient(page):
    rkey = page.send(CLARIFY_PLAIN)
    return f"/api/chat/responses/{rkey}/recipient", {"choice": "c0"}


def op_chat_stop(page):
    page.send(YELLOW)
    return "/api/chat/stop", {}


def op_handoff(page):
    page.send(YELLOW)
    return "/api/chat/handoff", {}


def op_cancel(page):
    rkey = page.send(YELLOW)
    return f"/api/device/tasks/T{rkey}/cancel", {}


def op_stop(page):
    page.send(YELLOW)
    return "/api/stop", {}


def op_query(page):
    return "/api/subscriptions/query", {}


def op_subscriptions_cancel(page):
    page.post("/api/subscriptions/query", {})
    assert finish(page, newest(page, "subscriptions")) == "done"
    version = page.listed("subscriptions")
    return "/api/subscriptions/cancel", {"choice": "c0", "list_version": version}


def op_billing(page):
    return "/api/billing/analyze", {}


def op_compare(page):
    return "/api/wealth/compare", {"objective": "net_return"}


def op_purchase(page):
    page.post("/api/wealth/compare", {"objective": "net_return"})
    return "/api/wealth/purchase", {"choice": None, "list_version": page.listed("wealth")}


def op_redeem(page):
    return "/api/wealth/redeem", {"choice": "c0", "list_version": page.listed("positions")}


def op_preset(page):
    return "/api/presets/suitability-r4", {}


# Every service function that opens a ``live.operation`` outside the two-phase commit: the
# route, its setup, the product call inside the operation, and whether a business card
# with a token is opened first (not where the route is that query itself or reuses it).
OPERATIONS = {
    "/api/session/login": (op_login, DemoController, "start_session", False),
    "/api/chat/message": (op_message, DomesticSession, "receive", True),
    "/api/chat/shortcut": (op_shortcut, DomesticSession, "receive", True),
    "/api/chat/responses/{rkey}/recipient": (op_recipient, DemoController, "select_contact", True),
    "/api/chat/stop": (op_chat_stop, DomesticSession, "receive", True),
    "/api/chat/handoff": (op_handoff, DomesticSession, "receive", True),
    "/api/device/tasks/{tkey}/cancel": (op_cancel, DomesticSession, "receive", True),
    "/api/stop": (op_stop, DemoController, "stop", True),
    "/api/subscriptions/query": (op_query, DemoController, "preview_subscriptions", False),
    "/api/subscriptions/cancel": (
        op_subscriptions_cancel,
        DemoController,
        "cancel_subscription",
        False,
    ),
    "/api/billing/analyze": (op_billing, compat, "load_billing", True),
    "/api/wealth/compare": (op_compare, DemoController, "compare_investments", True),
    "/api/wealth/purchase": (op_purchase, DemoController, "preview_investment", True),
    "/api/wealth/redeem": (op_redeem, DemoController, "redeem", True),
    "/api/presets/suitability-r4": (op_preset, compat, "prepare_demo", True),
}


def raise_after(monkeypatch, owner, name, raised):
    """``owner.name`` runs as it is, then raises: the call changed what it changes first."""
    original = getattr(owner, name)
    ran = []

    def changed_then_raised(*args, **kwargs):
        ran.append(original(*args, **kwargs))
        raise raised()

    monkeypatch.setattr(owner, name, changed_then_raised)
    return ran


def run_operation(web_app, monkeypatch, route, raised):
    setup, owner, name, opens = OPERATIONS[route]
    page = Browser(web_app)
    if route != "/api/session/login":
        page.login()
    if opens:
        open_query(page)
    path, body = setup(page)
    saved = held(page)
    ran = raise_after(monkeypatch, owner, name, raised)
    data = page.post(path, body, expect=500)
    assert len(ran) == 1, route
    assert not opens or saved, route
    assert_distrusted(page, data, saved, monkeypatch)


@pytest.mark.parametrize("route", list(OPERATIONS))
def test_i2_a_refusal_from_inside_an_operation_is_an_unknown_result(web_app, monkeypatch, route):
    """``live.operation``: the product call inside it ran, then raised a refusal. It is
    never answered as a refusal that changed nothing: an unknown result, the session
    distrusted (the operation's own expected failures, ``FAILURES``, are unchanged)."""
    run_operation(web_app, monkeypatch, route, lambda: errors.error("not_applicable"))


@pytest.mark.parametrize("raised", list(WEB_ERRORS.values()), ids=list(WEB_ERRORS))
def test_i2_any_web_error_from_inside_an_operation_is_an_unknown_result(
    web_app, monkeypatch, raised
):
    """Every ``WebError`` class of R4 raised from inside a message's operation."""
    run_operation(web_app, monkeypatch, "/api/chat/message", raised)


def red_at_mfa_request(page):
    open_query(page)
    red = "T" + page.send(RED)
    page.device(red, "confirm")
    return red, held(page)


def test_i2_a_code_delivery_failure_beyond_the_known_three_is_an_unknown_result(
    browser, monkeypatch
):
    """``device_mfa_request``: the code was requested, then delivering its simulated SMS
    raised a refusal. Only ``ValueError``, ``KeyError`` and ``RuntimeError`` are ignored
    (the closing refresh delivers again); anything else is an unknown result."""
    red, saved = red_at_mfa_request(browser)
    pending = browser.ws.tasks[red].identity

    def broken(*args, **kwargs):
        raise errors.error("not_applicable")

    monkeypatch.setattr(service, "deliver", broken)
    data = browser.post(f"/api/device/tasks/{red}/mfa/request", saved[red], expect=500)
    assert compat.pending_entry(browser.ws.controller, pending)["challenge_id"]
    assert_distrusted(browser, data, saved, monkeypatch)


@pytest.mark.parametrize("raised", [ValueError, KeyError, RuntimeError])
def test_i2_a_known_code_delivery_failure_is_still_delivered_by_the_refresh(
    browser, monkeypatch, raised
):
    """The deliberate exception, unchanged: the closing refresh delivers the code."""
    red, saved = red_at_mfa_request(browser)

    def broken(*args, **kwargs):
        raise raised("delivery")

    monkeypatch.setattr(service, "deliver", broken)
    data = browser.post(f"/api/device/tasks/{red}/mfa/request", saved[red])
    assert data["result"] == {"status": "waiting_mfa"} and not browser.ws.untrusted
    assert browser.sms_code() is not None
    assert browser.task(red)["gesture"]["op"] == "mfa_verify"


def test_i2_a_refusal_from_inside_the_recipient_checks_answer_is_an_unknown_result(
    browser, monkeypatch
):
    """``gate_answer``: the controller recorded the answers, then raised a refusal."""
    open_query(browser)
    rkey = browser.send(NEW)
    body = {"gate_version": browser.gate(rkey)["version"], "answers": SAFE}
    saved = held(browser)
    ran = raise_after(
        monkeypatch, DemoController, "scam_gate_answer", lambda: errors.error("not_applicable")
    )
    data = browser.post(f"/api/chat/responses/{rkey}/gate/answer", body, expect=500)
    assert len(ran) == 1 and saved
    assert_distrusted(browser, data, saved, monkeypatch)


def test_i2_a_refusal_from_inside_the_recipient_checks_retry_is_an_unknown_result(
    browser, monkeypatch
):
    """``gate_retry``: the controller reopened the paused check, then raised a refusal."""
    open_query(browser)
    rkey = browser.send(NEW)
    browser.answer(rkey, {"verified": "yes", "guided": "yes"})
    body = {"gate_version": browser.gate(rkey)["version"]}
    saved = held(browser)
    ran = raise_after(
        monkeypatch, DemoController, "scam_gate_retry", lambda: errors.error("not_applicable")
    )
    data = browser.post(f"/api/chat/responses/{rkey}/gate/retry", body, expect=500)
    assert len(ran) == 1 and saved
    assert_distrusted(browser, data, saved, monkeypatch)


def test_i2_a_refusal_while_a_received_reply_is_recorded_is_an_unknown_result(browser, monkeypatch):
    """``_submit``: the reply was received and the vault wrote the words, then a refusal
    was raised while the reply was recorded (outside the operation)."""
    open_query(browser)
    saved = held(browser)
    ran = raise_after(monkeypatch, compat, "vault_tokenize", lambda: errors.error("not_found"))
    data = browser.post("/api/chat/message", {**AFTER, "client_msg_id": "r5-tail-0001"}, expect=500)
    assert len(ran) == 1 and saved
    assert_distrusted(browser, data, saved, monkeypatch)


def test_i2_the_commit_already_treats_a_refusal_from_inside_the_call_as_unknown():
    """No change was needed in ``tasks.commit``: any ``WebError`` from inside the device
    call, a refusal included, closes the task as unknown (only ``operation_failed`` is an
    expected failure)."""
    harness = logged_in()
    tkey = "T" + harness.send(YELLOW)
    task = harness.ws.tasks[tkey]
    with pytest.raises(errors.WebOutcomeUnknown):
        with tasks.commit(harness.ws, task, "domestic"):
            raise errors.error("not_applicable")
    assert task.phase == "closed_unknown" and tkey not in harness.ws.gestures


# --- RW-4'''' item 3, E-15: the check fails with a lock that does not exclude --------------


class NonExclusiveLock(RecordedLock):
    """Records every call like ``RecordedLock`` and says truthfully whether a job holds it,
    but never makes a job wait: what a lock that failed to cover the job would do."""

    def __init__(self, lock, log):
        super().__init__(lock, log)
        self.holders = 0

    def locked(self):
        return self.holders > 0

    async def __aenter__(self):
        self.log.append(("acquire", JOB.get(), self.holders > 0))
        self.holders += 1
        self.log.append(("acquired", JOB.get(), id(self.lock)))

    async def __aexit__(self, *exc):
        self.holders -= 1
        self.log.append(("released", JOB.get(), id(self.lock)))
        return False


@pytest.mark.parametrize("first", ["live", "main"])
def test_i3_e15_the_two_pool_check_fails_when_the_lock_does_not_exclude(first):
    """RW-4'''' item 3: the same two-pool scenario and the same check as
    ``test_review_r4.py::test_e15_one_sessions_jobs_in_the_two_pools_never_overlap``, with
    the session's lock replaced by one that does not exclude. The check fails, and it
    fails on what a broken lock does: job two is granted the lock while job one holds it."""
    ws = web_state.new_web_session("customer", "zh-CN", browser_id="b" * 43, tab_id="t" * 32)
    seen = two_pool_record(ws, first, lock=NonExclusiveLock)
    with pytest.raises(AssertionError):
        assert_one_lock_serializes(seen)
    lock = seen["lock"]
    assert seen["log"] == [
        ("acquire", "one", False),
        ("acquired", "one", lock),
        ("acquire", "two", True),
        ("acquired", "two", lock),
    ]
    assert ("return", "one") not in [entry[:2] for entry in seen["jobs"]]
    final = seen["final"]
    assert final.index(("acquired", "two", lock)) < final.index(("released", "one", lock))


# --- RW-4'''' item 3, E-14: every counter counts a positive calibration ---------------------


@contextmanager
def counting(walk, state):
    """The walk's own counters, live under ``state``, around a direct call."""
    walk.counts[state] = Counter()
    walk.now = state
    try:
        yield walk.counts[state]
    finally:
        walk.now = None


def measured(walk, page, path, body):
    """One request counted by the walk's own instruments: ``Walk.measure`` (the POST walks
    of R3) or ``ReadWalk.read`` (the read walks of R4, here around a POST)."""
    if isinstance(walk, ReadWalk):
        walk.read("calibration", page, path, body=body)
    else:
        walk.measure("calibration", page, path, body)
    return walk.counts["calibration"]


def red_task(page, *steps):
    tkey = "T" + page.send(RED)
    for step in steps:
        page.device(tkey, step)
    return tkey


def calibrate(walk, counter):
    """What makes the device or the gateway do what ``counter`` counts, once."""
    page = walk.page()
    if counter in ("approve", "confirm_input", "dispatch"):
        tkey = "T" + page.send(YELLOW)
        return measured(walk, page, f"/api/device/tasks/{tkey}/confirm", held_body(page, tkey))
    if counter == "request_mfa":
        tkey = red_task(page, "confirm")
        return measured(walk, page, f"/api/device/tasks/{tkey}/mfa/request", held_body(page, tkey))
    if counter == "submit_mfa":
        tkey = red_task(page, "confirm", "mfa_request")
        body = {**held_body(page, tkey), "code": page.sms_code()}
        return measured(walk, page, f"/api/device/tasks/{tkey}/mfa/verify", body)
    if counter == "green":
        return measured(walk, page, "/api/billing/analyze", {})
    assert counter == "complete_mfa"
    # The web never lets the device fill in a code (INV-2.2), so no route can calibrate
    # this one: the controller's own device-filled path is called directly.
    tkey = red_task(page, "confirm", "mfa_request")
    identity = page.ws.tasks[tkey].identity
    with counting(walk, "calibration") as counts, bound(page.ws):
        assert page.ws.controller.complete_mfa(identity)["status"] == "mfa_verified"
    return counts


# At least this many per calibration ("+": one or more).
CALIBRATED = {
    "approve": 1,
    "confirm_input": "+",
    "complete_mfa": 1,
    "request_mfa": 1,
    "submit_mfa": 1,
    "green": "+",
    "dispatch": 1,
}


def test_i3_e14_every_counter_has_a_calibration():
    assert tuple(CALIBRATED) == COUNTED
    assert set(READ_COUNTED) - set(COUNTED) == set(READ_ONLY)


@pytest.mark.parametrize("instrument", [Walk, ReadWalk], ids=["post-walk", "read-walk"])
@pytest.mark.parametrize("counter", COUNTED)
def test_i3_e14_each_counter_counts_its_positive_calibration(
    web_app, monkeypatch, tmp_path, counter, instrument
):
    """RW-4'''' item 3: each of ``COUNTED``'s counters, through the very spies the E-14
    walks use (R3's ``Walk`` for the POST routes, R4's ``ReadWalk`` for the reads), counts
    the call it names: a zero in those tests is a call that did not happen."""
    walk = instrument(web_app, "calibration", monkeypatch, tmp_path)
    counts = calibrate(walk, counter)
    want = CALIBRATED[counter]
    if want == "+":
        assert counts[counter] >= 1, (counter, counts)
    else:
        assert counts[counter] == want, (counter, counts)


# The read walks' own counters besides ``COUNTED`` (what a POST does and a read never may).
READ_ONLY = ("refresh", "issue", "live_sync", "revision", "tokens")


@pytest.mark.parametrize("counter", READ_ONLY)
def test_i3_e14_each_read_counter_counts_a_business_post(web_app, monkeypatch, tmp_path, counter):
    """``ReadWalk``'s other counters: a business POST measured with the same bookkeeping
    refreshes, issues a token, syncs the live switch, steps the revision and changes the
    held tokens, and each counter sees it."""
    walk = ReadWalk(web_app, "calibration", monkeypatch, tmp_path)
    page = walk.page()
    walk.read("calibration", page, "/api/chat/message", body={**AFTER, "client_msg_id": "r5-cal-1"})
    counts = walk.counts["calibration"]
    assert counts[counter] >= 1, (counter, counts)
    assert counts["live_sync"] == 2 and counts["revision"] == 1


# --- RW-4'''' item 5: the answers below routing that a closed lab path can get --------------


def lab_head(page, path, *, hosts=1, length=2):
    lines = [
        f"POST {path} HTTP/1.1".encode(),
        *[f"Host: {page.client.host}".encode()] * hosts,
        f"Origin: {page.client.origin}".encode(),
        b"Content-Type: application/json",
        f"Cookie: corridor_web={page.client.browser}".encode(),
        f"Content-Length: {length}".encode(),
        b"Connection: close",
    ]
    return b"\r\n".join(lines) + b"\r\n\r\n"


def test_i5_below_routing_a_lab_path_gets_tornados_400_or_405_and_nothing_runs(
    browser, monkeypatch
):
    """ "Every ``/api/lab*`` request answers 404" holds for every request that reaches the
    application's routing (``Base.prepare``). Below it tornado answers two kinds itself,
    for every path alike: 400 for malformed HTTP or a body above the 128 KiB entry (a
    repeated Host, a declared 300 KiB body) and 405 for a method outside its supported set
    (PROPFIND, TRACE). None of them runs a step past routing or touches a session."""
    assert lab_head(browser, LAB_GET).startswith(b"POST /api/lab HTTP/1.1")
    seen = traced(monkeypatch)
    ws = browser.ws
    revision = ws.revision
    client = browser.client
    lines = [("Host", client.host), ("Origin", client.origin), *client.cookie().items()]
    lines = [(name, value) for name, value in lines if value is not None]
    for path in (LAB_GET, *LAB_POSTS, *LAB_OTHER_PATHS):
        for method in ("PROPFIND", "TRACE"):
            response = client.request(method, path, b"{}", lines)
            assert response.code == 405, (method, path, response.code)
            assert client.json(response)["error"]["code"] == "method_not_allowed"
        answer = raw_request(client.port, lab_head(browser, path, hosts=2), b"{}")
        assert answer == b"" or answer.startswith(b"HTTP/1.1 400"), (path, answer[:40])
        answer = raw_request(client.port, lab_head(browser, path, length=300 * 1024))
        assert answer.startswith(b"HTTP/1.1 400"), (path, answer[:40])
        # A supported method reaches routing, where the closed lab answers 404.
        assert client.request("PUT", path, b"{}", lines).code == 404
    assert seen == [] and ws.revision == revision and ws.busy == 0 and not ws.untrusted
