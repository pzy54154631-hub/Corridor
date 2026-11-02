"""Regression tests for the sixth review round (R6) of the web front end.

Each test names the item it closes:

- ``test_i1_*`` (item 1, safety): the bookkeeping that closes a ``live.operation`` (after
  the call succeeded: the attempts, their summary, the reply's note and the planner log;
  after an expected failure: the failure record and the page notice) and the recording
  that follows a prepared chat card (``choose_subscription``, ``choose_schedule_cancel``,
  ``complete_aa``, and ``choose_birthday``'s registration of the draft). A refusal or a
  contained result raised there is an unknown result: the session is distrusted, and
  every valid token held before is refused at confirm, mfa/request and mfa/verify.
- ``test_i2_*`` (item 2, the structural fallback): the session's product objects are
  proxies (``state.Product``) that count every method call outside a read-only allow
  list. For every Write route, a refusal (409) or a contained result raised after the
  route's first product write is an unknown result and the session is distrusted; raised
  before any product write, it keeps its status; a route that writes no product state
  keeps its answer. The allow list is the reviewed one, each entry leaves the product
  state unchanged, the proxies and the count are checked directly, and every product
  call the web layer makes on a route is either on the allow list or passes a proxy.
- ``test_i3_*`` (item 3 c, the R6 review's follow-ups): the three double-fault scenarios
  start from a red card at ``mfa_request`` with a valid token, with a no-fault control
  that sends a code from that token; ``assert_distrusted`` sees the gate records filled
  before the fault and gone after it; the six spied entry points each count a positive
  calibration.

The real handlers, service functions, controllers, gateway and kernel run throughout,
except where a test injects the fault it names. This module runs the
submission build (``FEATURES`` as shipped); the AA and lab routes open their capability
explicitly, and the live routes configure the test-only fake deployment.
"""

import dataclasses
import functools
import inspect
import sys
from collections import Counter
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

import corridor.web as web_package
from corridor.agent.domestic_session import DomesticSession
from corridor.ui.controller import DemoController
from corridor.ui.live_config import DEPLOYMENTS
from corridor.ui.live_runtime import access_proof
from corridor.ui.wealth_controller import WealthController
from corridor.web import compat, errors, handlers, lab, live, service, suggest, tasks
from corridor.web import state as web_state
from corridor.web.executor import bound
from tests.suggest_fakes import TRANSFER, UNCLEAR, suggest_rule
from tests.unit.test_ui_live_mode import ACCESS_CODE, FakeModel, environ
from tests.unit.test_ui_scam_friction import CLARIFY_PLAIN, NEW, SAFE
from tests.web.conftest import serve
from tests.web.test_api_chat import Browser
from tests.web.test_lab import current as lab_current
from tests.web.test_review_r1 import finish, held_body, newest, spied
from tests.web.test_review_r3 import PURCHASE, dispatched_plan, nonce, open_query, unlocked
from tests.web.test_review_r5 import (
    CLOSING,
    Closing,
    held,
    raise_after,
    schedule_choice,
    subscription_choice,
)
from tests.web.test_tasks import BIRTHDAY, RED, YELLOW

# The device's and the gateway's entry points that an old token must never reach again.
COUNTERS = ("approve", "confirm_input", "complete_mfa", "request_mfa", "submit_mfa", "dispatch")
AFTER = {"text": YELLOW, "external_files": 0, "client_msg_id": "r6-after-0001"}
BALANCE = "查询余额"
AA_TEXT = "这顿饭460我们四个AA"
AA = "/api/chat/responses/{rkey}/aa"
WEB_DIR = Path(web_state.__file__).resolve().parent
PROXY_FILE = Path(web_state.__file__).resolve()
# The read-only allow list as reviewed for RW-5 item 2 (each entry's reason: state.py).
READS = {
    "DemoController": frozenset({
        "snapshot",
        "scam_gate_status",
        "kernel_refusal",
        "risk_retake_blocked",
        "at_transaction_boundary",
        "_authoritative_cards",
        "_registered_plan_ids",
        "_scam_grade",
    }),
    "DomesticSession": frozenset({"response", "aa_roster", "purchase_review_terms"}),
    "WealthController": frozenset(),
}


def refusal():
    return errors.error("not_applicable")


# What is injected: a refusal (409), or the lab's contained unknown result.
FAULTS = {"409": refusal, "contained": errors.WebChildOutcomeUnknown}


@pytest.fixture
def browser(web_app):
    page = Browser(web_app)
    page.login()
    return page


def target(obj):
    """The product object behind a session's proxy (the object itself where there is none)."""
    return getattr(obj, "_target", obj)


def assert_distrusted(page, data, saved, monkeypatch, *, code=None, error="outcome_unknown"):
    """R5's check with the R6 follow-ups: the answer carries the call's own result and the
    state; the session is distrusted; every token, gate evaluation and gate record is
    gone (the gate records were filled before the fault, item 3 c). No token held before
    the failure approves, signs an input, sends a code, submits a code or dispatches, and
    no new request is accepted; a later read shows no token either."""
    assert data["ok"] is False and data["error"]["code"] == error, data
    ws = page.ws
    assert ws.untrusted is True
    assert ws.gestures == {} and ws.gate_eval == {} and ws.gates == {}
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
    assert ws.untrusted is True and ws.gestures == {} and ws.gates == {}
    state = page.get("/api/state")["state"]
    assert all(task["gesture"] is None for task in state["device"]["tasks"])


def red_business_card(page):
    """A red business card (a wealth purchase) at the ``mfa_request`` step with a valid
    token. No chat reply owns it, so the token stays valid while the chat moves on."""
    page.post("/api/wealth/compare", {"objective": "net_return"})
    page.post("/api/wealth/purchase", {"choice": None, "list_version": page.listed("wealth")})
    (task,) = [task for task in page.state["device"]["tasks"] if task["source"] == "wealth"]
    assert task["step"] == "mfa_request" and task["gesture"]["op"] == "mfa_request"
    return task["key"], held_body(page, task["key"])


def gated(page):
    """A reply with a recipient check, so the session holds a gate record (item 3 c)."""
    rkey = page.send(NEW)
    assert page.gate(rkey)["version"] and page.ws.gates
    return rkey


def go_live(page, monkeypatch, tmp_path):
    """The live switch on, behind the test-only fake model (no socket is opened): the next
    business POST attaches the live client, and each operation then records the calls it
    made (``live.py``'s success tail: the attempts, their summary, the reply's note)."""
    for name, value in environ(tmp_path).items():
        monkeypatch.setenv(name, value)
    FakeModel(monkeypatch)
    unlocked(page)
    assert page.post("/api/live/toggle", {"on": True})["live"]["toggle"]["on"] is True


# --- RW-5 item 1: the bookkeeping that closes an operation ------------------------------------


class Exploding(list):
    """``ws.op_log`` that raises on ``append`` once armed: the one step of the success tail
    a scripted operation performs (no live call to index or summarize)."""

    def __init__(self, items, raised):
        super().__init__(items)
        self.raised = raised
        self.armed = False
        self.hits = 0

    def append(self, item):
        if self.armed:
            self.hits += 1
            raise self.raised()
        super().append(item)


def arm_after(monkeypatch, owner, name, arm):
    """``owner.name`` runs as it is, then ``arm()`` is called: what the product call left
    behind is in place when the step after it fails."""
    original = getattr(owner, name)
    ran = []

    def changed_then_armed(*args, **kwargs):
        ran.append(original(*args, **kwargs))
        arm()
        return ran[-1]

    monkeypatch.setattr(owner, name, changed_then_armed)
    return ran


def raise_once_after(monkeypatch, owner, name, raised):
    """``owner.name`` runs as it is and raises once, on its first call (a label the view
    models read again while the answer's state is built); later calls run as before."""
    original = getattr(owner, name)
    ran = []

    def changed_then_raised_once(*args, **kwargs):
        result = original(*args, **kwargs)
        if not ran:
            ran.append(result)
            raise raised()
        return result

    monkeypatch.setattr(owner, name, changed_then_raised_once)
    return ran


# Where the success tail of ``live.operation`` is cut: the step, and whether it is only
# reached with the live client attached.
TAIL = {
    "outcomes": (live, "_outcomes", True),
    "summary": (compat, "_summary", True),
    "remember": (live, "_remember", True),
    "op_log": (None, None, False),
}


@pytest.mark.parametrize("fault", list(FAULTS))
@pytest.mark.parametrize("cut", list(TAIL))
def test_i1_a_refusal_from_the_operations_success_tail_is_an_unknown_result(
    browser, monkeypatch, tmp_path, cut, fault
):
    """``/api/chat/message``: the reply was received (the product session holds it), then
    the bookkeeping that closes the operation raised a refusal or a contained result. An
    unknown result: the session is distrusted, the red card's token sends no code."""
    owner, name, hosted = TAIL[cut]
    if hosted:
        go_live(browser, monkeypatch, tmp_path)
    red, _ = red_business_card(browser)
    saved = held(browser)
    assert red in saved
    history = len(target(browser.ws.session).history)
    if owner is None:
        exploding = browser.ws.op_log = Exploding(browser.ws.op_log, FAULTS[fault])
        arm_after(
            monkeypatch, DomesticSession, "receive", lambda: setattr(exploding, "armed", True)
        )
    else:
        ran = raise_after(monkeypatch, owner, name, FAULTS[fault])
    body = {"text": BALANCE, "external_files": 0, "client_msg_id": "r6-tail-0001"}
    data = browser.post("/api/chat/message", body, expect=500)
    if owner is None:
        exploding.armed = False
        assert exploding.hits == 1
    else:
        assert len(ran) == 1
    # The reply exists in the product session; the page never recorded it.
    assert len(target(browser.ws.session).history) == history + 1
    assert_distrusted(browser, data, saved, monkeypatch)
    assert browser.sms_code() is None


# Where the bookkeeping of an expected failure is cut.
FAILED_CLOSING = {"notice": (live, "_notice", False), "outcomes": (live, "_outcomes", True)}


@pytest.mark.parametrize("fault", list(FAULTS))
@pytest.mark.parametrize("cut", list(FAILED_CLOSING))
def test_i1_a_refusal_from_the_operations_failure_bookkeeping_is_an_unknown_result(
    browser, monkeypatch, tmp_path, cut, fault
):
    """``/api/subscriptions/query``: the controller prepared the query's card, then the
    call raised one of the operation's expected failures (``FAILURES``); recording that
    failure raised a refusal or a contained result. An unknown result, never a refusal
    and never the expected failure's 422."""
    owner, name, hosted = FAILED_CLOSING[cut]
    if hosted:
        go_live(browser, monkeypatch, tmp_path)
    red, _ = red_business_card(browser)
    saved = held(browser)
    c = target(browser.ws.controller)
    pending = set(c._pending)
    prepared = raise_after(
        monkeypatch, DemoController, "preview_subscriptions", lambda: RuntimeError("expected")
    )
    ran = raise_after(monkeypatch, owner, name, FAULTS[fault])
    data = browser.post("/api/subscriptions/query", {}, expect=500)
    assert len(prepared) == 1 and set(c._pending) - pending
    assert len(ran) == 1 and red in saved
    assert_distrusted(browser, data, saved, monkeypatch)
    assert browser.sms_code() is None


# --- RW-5 item 1: the recording after a prepared chat card --------------------------------------

# The two chat choices: the setup, the preparer (the witness that a card was prepared) and
# the steps of the recording that follows it.
RECORDING = {
    "subscription": (
        subscription_choice,
        "prepare_subscription_cancel",
        {"register": (service, "_register"), "turn": (service, "_turn")},
        (compat, "subscription_object"),
    ),
    "schedule-cancel": (
        schedule_choice,
        "prepare_schedule_cancel",
        {"register": (service, "_register"), "turn": (service, "_turn")},
        (compat, "schedule_object"),
    ),
}


@pytest.mark.parametrize("fault", list(FAULTS))
@pytest.mark.parametrize("cut", ["register", "turn", "label"])
@pytest.mark.parametrize("choice", list(RECORDING))
def test_i1_a_refusal_while_a_prepared_choice_is_recorded_is_an_unknown_result(
    browser, monkeypatch, choice, cut, fault
):
    """The preparer returned a new card; the reply's key, its turns or its object's label
    then raised a refusal or a contained result. An unknown result, the session
    distrusted, the business card's token refused."""
    setup, preparer, cuts, label = RECORDING[choice]
    open_query(browser)
    path, body = setup(browser)
    saved = held(browser)
    c = target(browser.ws.controller)
    pending = set(c._pending)
    prepared = arm_after(monkeypatch, DomesticSession, preparer, lambda: None)
    if cut == "label":
        ran = raise_once_after(monkeypatch, *label, FAULTS[fault])
    else:
        ran = raise_after(monkeypatch, *cuts[cut], FAULTS[fault])
    data = browser.post(path, body, expect=500)
    assert len(prepared) == 1 and set(c._pending) - pending and len(ran) == 1
    assert saved
    assert_distrusted(browser, data, saved, monkeypatch)


@pytest.fixture
def aa_page():
    # Keep the explicit feature setting for both default and opt-in deployments.
    with serve({"features": web_package.FEATURES | {"aa"}}) as app:
        page = Browser(app)
        page.login()
        yield page


AA_CUTS = {
    "register": (service, "_register"),
    "turn": (service, "_turn"),
    "label": (compat, "reference_label"),
}


@pytest.mark.parametrize("fault", list(FAULTS))
@pytest.mark.parametrize("cut", list(AA_CUTS))
def test_i1_a_refusal_while_a_completed_split_bill_is_recorded_is_an_unknown_result(
    aa_page, monkeypatch, cut, fault
):
    """``complete_aa``: ``prepare_aa`` returned the new reply; recording it raised."""
    open_query(aa_page)
    rkey = aa_page.send(AA_TEXT)
    saved = held(aa_page)
    history = len(target(aa_page.ws.session).history)
    inject = raise_once_after if cut == "label" else raise_after
    ran = inject(monkeypatch, *AA_CUTS[cut], FAULTS[fault])
    data = aa_page.post(AA.format(rkey=rkey), {"choices": ["c0", "c1", "c2"]}, expect=500)
    assert len(target(aa_page.ws.session).history) == history + 1 and len(ran) == 1
    assert saved
    assert_distrusted(aa_page, data, saved, monkeypatch)


def birthday_choice(page):
    rkey = page.send("帮我准备生日")
    choice = next(
        item["key"] for item in page.response(rkey)["birthday"]["choices"] if item["enabled"]
    )
    return f"/api/chat/responses/{rkey}/birthday", {"choice": choice}


@pytest.mark.parametrize("fault", list(FAULTS))
def test_i1_a_refusal_while_the_birthday_draft_is_registered_is_an_unknown_result(
    browser, monkeypatch, fault
):
    """``choose_birthday``: the preparer returned the draft (a plan is registered); the
    page's registration of the draft's task raised."""
    open_query(browser)
    path, body = birthday_choice(browser)
    saved = held(browser)
    c = target(browser.ws.controller)
    plans = len(c._plans)
    ran = raise_after(monkeypatch, service, "_business", FAULTS[fault])
    data = browser.post(path, body, expect=500)
    assert len(c._plans) == plans + 1 and len(ran) == 1
    assert saved
    assert_distrusted(browser, data, saved, monkeypatch)


# --- RW-5 item 2: the structural fallback, route by route -------------------------------------


class Calls:
    """Every method call on a product object while installed: the class, the method, the
    object and the file of the immediate caller. Installed on the product classes, so a
    call that bypassed the session's proxies is recorded too."""

    def __init__(self, monkeypatch):
        self.seen = []
        for cls in (DemoController, WealthController, DomesticSession):
            for name, member in list(vars(cls).items()):
                if name.startswith("__") or not inspect.isfunction(member):
                    continue
                monkeypatch.setattr(cls, name, self._recorded(name, member))

    def _recorded(self, name, member):
        seen = self.seen

        @functools.wraps(member)
        def recorded(obj, *args, **kwargs):
            seen.append((type(obj).__name__, name, id(obj), sys._getframe(1).f_code.co_filename))
            return member(obj, *args, **kwargs)

        return recorded


def web_caller(filename):
    """Whether the immediate caller is a module of the web layer (the proxy included)."""
    try:
        return Path(filename).resolve().is_relative_to(WEB_DIR)
    except (OSError, ValueError):
        return False


def web_writes(records, ws):
    """The product calls the web layer made on this session's own objects outside the
    allow list, directly or through the proxies."""
    parents = {id(target(ws.controller)), id(target(ws.session)), id(target(ws.wealth))}
    return [
        (cls, name)
        for cls, name, obj, filename in records
        if obj in parents and name not in READS[cls] and web_caller(filename)
    ]


def strays(records):
    """The product calls made directly by a web module other than the proxy's that are
    not on the allow list: calls the count never saw."""
    return [
        (cls, name, Path(filename).name)
        for cls, name, _, filename in records
        if web_caller(filename)
        and Path(filename).resolve() != PROXY_FILE
        and name not in READS[cls]
    ]


class Action:
    """The route's service function, as the handler calls it, wrapped: the product calls
    made while it ran are ``records`` (the closing steps' calls are not); it may then
    raise, or be replaced altogether."""

    def __init__(self, monkeypatch, calls, owner, name, *, raised=None, replacement=None):
        original = getattr(owner, name)
        self.calls = calls
        self.ran = []
        self.window = None

        def wrapped(*args, **kwargs):
            start = len(calls.seen)
            try:
                result = (replacement or original)(*args, **kwargs)
            finally:
                self.window = (start, len(calls.seen))
            self.ran.append(result)
            if raised is not None:
                raise raised()
            return result

        monkeypatch.setattr(owner, name, wrapped)

    @property
    def records(self):
        start, end = self.window
        return self.calls.seen[start:end]


@dataclass(frozen=True)
class Route:
    """One Write route: its setup, the function the handler calls, and what it does."""

    setup: Callable[[Browser], tuple[str, dict]]
    owner: Any
    name: str
    # The positive path calls a product method outside the allow list on this session.
    writes: bool
    login: bool = True
    # When a business card with a token is opened: before the setup, after it (the setup's
    # chat Stop would retire it), or never (the route is that query or reuses it, or needs
    # the transaction boundary).
    opens: str | None = "before"
    features: frozenset = frozenset()
    # The live switch's test-only deployment is configured first.
    hosted: bool = False
    # The fake deployment's rule (``FakeModel(rule=...)``): what its suggestion step answers.
    rule: Callable | None = None
    # Run after the login and before any card is opened: the live switch needs the
    # transaction boundary, and the suggestion route needs the switch on.
    prelude: Callable[[Browser], None] | None = None


def r_login(page):
    return "/api/session/login", {}


def r_view(page):
    return "/api/session/view", {"view": "customer"}


def r_locale(page):
    return "/api/session/locale", {"locale": "en"}


def r_message(page):
    return "/api/chat/message", {"text": YELLOW, "external_files": 0, "client_msg_id": "r6-r-0001"}


def r_shortcut(page):
    return "/api/chat/shortcut", {"shortcut": "transfer", "client_msg_id": "r6-r-0001"}


def r_current(page):
    older = page.send(YELLOW)
    page.send(BALANCE)
    return f"/api/chat/responses/{older}/current", {}


def r_recipient(page):
    rkey = page.send(CLARIFY_PLAIN)
    return f"/api/chat/responses/{rkey}/recipient", {"choice": "c0"}


def live_on(page):
    """The live switch on behind the fake deployment, at the transaction boundary."""
    unlocked(page)
    assert page.post("/api/live/toggle", {"on": True})["live"]["toggle"]["on"] is True


def r_suggestion(page):
    """A plain clarification under which the fake live model suggested one transfer."""
    rkey = page.send(UNCLEAR)
    offered = page.response(rkey)["suggestion_select"]
    assert [item["state"] for item in offered["choices"]] == ["offered"]
    body = {"choice": "c0", "list_version": offered["list_version"], "client_msg_id": "r6-sg-0001"}
    return f"/api/chat/responses/{rkey}/suggestion", body


def r_gate_answer(page):
    rkey = page.send(NEW)
    body = {"gate_version": page.gate(rkey)["version"], "answers": SAFE}
    return f"/api/chat/responses/{rkey}/gate/answer", body


def r_gate_retry(page):
    rkey = page.send(NEW)
    page.answer(rkey, {"verified": "yes", "guided": "yes"})
    return f"/api/chat/responses/{rkey}/gate/retry", {"gate_version": page.gate(rkey)["version"]}


def r_aa(page):
    return AA.format(rkey=page.send(AA_TEXT)), {"choices": ["c0", "c1", "c2"]}


def r_chat_stop(page):
    page.send(YELLOW)
    return "/api/chat/stop", {}


def r_handoff(page):
    page.send(YELLOW)
    return "/api/chat/handoff", {}


def r_compensate(page):
    dispatched_plan(page)
    version = page.tab("operator")["compensation"]["compensate_version"]
    return "/api/chat/compensate", {"plan_version": version}


def r_nodes(page):
    plan = "T" + page.send(BIRTHDAY)
    body = {"card_version": page.task(plan)["nodes_version"], "nodes": ["n0", "n1"]}
    return f"/api/device/tasks/{plan}/nodes", body


def r_confirm(page):
    tkey = "T" + page.send(YELLOW)
    return f"/api/device/tasks/{tkey}/confirm", held_body(page, tkey)


def r_mfa_request(page):
    tkey = "T" + page.send(RED)
    page.device(tkey, "confirm")
    return f"/api/device/tasks/{tkey}/mfa/request", held_body(page, tkey)


def r_mfa_verify(page):
    tkey = "T" + page.send(RED)
    page.device(tkey, "confirm")
    page.device(tkey, "mfa_request")
    return f"/api/device/tasks/{tkey}/mfa/verify", {
        **held_body(page, tkey),
        "code": page.sms_code(),
    }


def r_cancel(page):
    rkey = page.send(YELLOW)
    return f"/api/device/tasks/T{rkey}/cancel", {}


def r_stop(page):
    page.send(YELLOW)
    return "/api/stop", {}


def r_query(page):
    return "/api/subscriptions/query", {}


def r_subscriptions_cancel(page):
    page.post("/api/subscriptions/query", {})
    assert finish(page, newest(page, "subscriptions")) == "done"
    return "/api/subscriptions/cancel", {
        "choice": "c0",
        "list_version": page.listed("subscriptions"),
    }


def r_billing(page):
    return "/api/billing/analyze", {}


def r_compare(page):
    return "/api/wealth/compare", {"objective": "net_return"}


def r_purchase(page):
    page.post("/api/wealth/compare", {"objective": "net_return"})
    return "/api/wealth/purchase", {"choice": None, "list_version": page.listed("wealth")}


def r_redeem(page):
    return "/api/wealth/redeem", {"choice": "c0", "list_version": page.listed("positions")}


def r_preset(page):
    return "/api/presets/suitability-r4", {}


def r_live_challenge(page):
    return "/api/live/challenge", {}


def r_live_unlock(page):
    value = nonce(page)
    return "/api/live/unlock", {"nonce": value, "proof": access_proof(ACCESS_CODE, value)}


def r_live_select(page):
    unlocked(page)
    return "/api/live/select", {"deployment": DEPLOYMENTS[1].id}


def r_live_toggle(page):
    unlocked(page)
    return "/api/live/toggle", {"on": True}


def lab_start(page):
    return page.post("/api/lab/start", {"scenario": "yellow", "mode": "scripted"})["lab"]


def r_lab_start(page):
    return "/api/lab/start", {"scenario": "yellow", "mode": "scripted"}


def r_lab_change(page):
    return "/api/lab/change", {"lab_generation": lab_start(page)["session_generation"]}


def r_lab_try_previous(page):
    vm = lab_start(page)
    vm = page.post("/api/lab/change", {"lab_generation": vm["session_generation"]})["lab"]
    return "/api/lab/try-previous", {"lab_generation": vm["session_generation"]}


def r_lab_gesture(page):
    vm = lab_start(page)
    task = lab_current(vm)
    shown = task["gesture"]
    body = {
        "lab_generation": vm["session_generation"],
        "tkey": task["key"],
        "op": shown["op"],
        "gesture": shown["token"],
        "card_version": shown["card_version"],
    }
    return "/api/lab/gesture", body


LAB = frozenset({lab.FEATURE})


def r_cards(page):
    from tests.web.test_public_completeness import card_body

    return "/api/cards/preview", card_body(page, "lower_card_limit", limit="500")


def r_risk(page):
    from tests.web.test_public_completeness import risk_form

    return "/api/wealth/risk", {"answers": [0] * 5, "version": risk_form(page)["list_version"]}


def r_risk_retake(page):
    return "/api/wealth/risk/retake", {
        "version": page.tab("wealth")["questionnaire"]["list_version"]
    }


ROUTES = {
    "/api/cards/preview": Route(r_cards, service, "cards_preview", True),
    # Prepare the repeat-assessment clock before taking the control card's snapshot.
    "/api/wealth/risk": Route(r_risk, service, "wealth_risk", True, opens="after"),
    "/api/wealth/risk/retake": Route(r_risk_retake, service, "wealth_risk_retake", True),
    "/api/lab/start": Route(r_lab_start, lab, "start", False, features=LAB),
    "/api/lab/change": Route(r_lab_change, lab, "change", False, features=LAB),
    "/api/lab/try-previous": Route(r_lab_try_previous, lab, "try_previous", False, features=LAB),
    "/api/lab/gesture": Route(r_lab_gesture, lab, "gesture", False, features=LAB),
    "/api/session/login": Route(r_login, service, "login", True, login=False, opens=None),
    "/api/session/view": Route(r_view, service, "set_view", False),
    "/api/session/locale": Route(r_locale, service, "set_locale", False),
    "/api/chat/message": Route(r_message, service, "send_message", True),
    "/api/chat/shortcut": Route(r_shortcut, service, "send_shortcut", True),
    "/api/chat/responses/{rkey}/current": Route(r_current, service, "make_current", False),
    "/api/chat/responses/{rkey}/recipient": Route(r_recipient, service, "select_recipient", True),
    # The desk re-checks the suggestion (counted) and sends its sentence (counted again).
    "/api/chat/responses/{rkey}/suggestion": Route(
        r_suggestion,
        suggest,
        "choose",
        True,
        hosted=True,
        rule=suggest_rule(TRANSFER),
        prelude=live_on,
    ),
    "/api/chat/responses/{rkey}/subscription": Route(
        subscription_choice, service, "choose_subscription", True
    ),
    "/api/chat/responses/{rkey}/schedule-cancel": Route(
        schedule_choice, service, "choose_schedule_cancel", True
    ),
    "/api/chat/responses/{rkey}/gate/answer": Route(r_gate_answer, service, "gate_answer", True),
    "/api/chat/responses/{rkey}/gate/retry": Route(r_gate_retry, service, "gate_retry", True),
    "/api/chat/responses/{rkey}/birthday": Route(birthday_choice, service, "choose_birthday", True),
    AA: Route(r_aa, service, "complete_aa", True, features=frozenset({"aa"})),
    "/api/chat/stop": Route(r_chat_stop, service, "chat_stop", True),
    "/api/chat/handoff": Route(r_handoff, service, "handoff", True),
    "/api/chat/compensate": Route(r_compensate, service, "compensate_latest", True, opens="after"),
    "/api/device/tasks/{tkey}/nodes": Route(r_nodes, service, "select_nodes", False),
    "/api/device/tasks/{tkey}/confirm": Route(r_confirm, service, "device_confirm", True),
    "/api/device/tasks/{tkey}/mfa/request": Route(
        r_mfa_request, service, "device_mfa_request", True
    ),
    "/api/device/tasks/{tkey}/mfa/verify": Route(r_mfa_verify, service, "device_mfa_verify", True),
    "/api/device/tasks/{tkey}/cancel": Route(r_cancel, service, "device_cancel", True),
    "/api/stop": Route(r_stop, service, "stop_all", True),
    "/api/subscriptions/query": Route(r_query, service, "subscriptions_query", True, opens=None),
    "/api/subscriptions/cancel": Route(
        r_subscriptions_cancel, service, "subscriptions_cancel", True, opens=None
    ),
    "/api/billing/analyze": Route(r_billing, service, "billing_analyze", True),
    "/api/wealth/compare": Route(r_compare, service, "wealth_compare", True),
    "/api/wealth/purchase": Route(r_purchase, service, "wealth_purchase", True),
    "/api/wealth/redeem": Route(r_redeem, service, "wealth_redeem", True),
    "/api/presets/suitability-r4": Route(r_preset, service, "preset_r4", False),
    # The live switch needs the transaction boundary: no card is open on these routes.
    "/api/live/challenge": Route(
        r_live_challenge, live, "challenge", False, opens=None, hosted=True
    ),
    "/api/live/unlock": Route(r_live_unlock, live, "unlock", False, opens=None, hosted=True),
    "/api/live/select": Route(r_live_select, live, "select", False, opens=None, hosted=True),
    "/api/live/toggle": Route(r_live_toggle, live, "toggle", False, opens=None, hosted=True),
}
WRITING = sorted(route for route, spec in ROUTES.items() if spec.writes)
QUIET = sorted(route for route, spec in ROUTES.items() if not spec.writes)


def test_i2_every_write_route_has_a_scenario():
    assert set(ROUTES) == set(handlers.POSTS)


class Scenario:
    """A page in the build a route needs, set up for the route: logged in unless the route
    is the login itself, with a business card's token held unless the route is that query
    or reuses it, the fake deployment configured for the live routes and switched on for
    the suggestion route (its prelude, before the card is opened)."""

    def __init__(self, route, monkeypatch, tmp_path):
        self.spec = spec = ROUTES[route]
        settings = {"features": web_package.FEATURES | spec.features} if spec.features else {}
        self._serving = serve(settings)
        self.app = self._serving.__enter__()
        if spec.hosted:
            for name, value in environ(tmp_path).items():
                monkeypatch.setenv(name, value)
            FakeModel(monkeypatch, rule=spec.rule)
        self.page = Browser(self.app)
        if spec.login:
            self.page.login()
        if spec.prelude is not None:
            spec.prelude(self.page)
        self.card = open_query(self.page) if spec.opens == "before" else None
        self.path, self.body = spec.setup(self.page)
        if spec.opens == "after":
            self.card = open_query(self.page)
        self.saved = held(self.page)
        assert self.saved or not self.card, route

    def close(self):
        self._serving.__exit__(None, None, None)


@pytest.fixture
def scenario(request, monkeypatch, tmp_path):
    built = Scenario(request.param, monkeypatch, tmp_path)
    try:
        yield built
    finally:
        built.close()


def refuse_after_reads(ws, *args, **kwargs):
    """What a route does before its first product write, generalized: the reads of every
    allow-listed kind (the snapshot, the boundary, the registered plans, each task's step
    and digest, the chat roster), then a refusal."""
    c = ws.controller
    c.snapshot()
    c.at_transaction_boundary()
    c._registered_plan_ids()
    for task in tasks.collect(ws):
        info = tasks.task_step(ws, task)
        if info.identity:
            tasks.digest(ws, task, info)
    ws.session.aa_roster()
    raise refusal()


@pytest.mark.parametrize("fault", list(FAULTS))
@pytest.mark.parametrize("scenario", WRITING, indirect=True)
def test_i2_after_the_first_product_write_a_refusal_is_an_unknown_result(
    scenario, monkeypatch, fault
):
    """Every Write route that calls a product method outside the allow list: the route's
    service function ran as it is (the product call happened), then a refusal or a
    contained result left ``action.run``. Whatever raised it, the answer is an unknown
    result and the session is distrusted: no token held before is honoured again."""
    spec, page = scenario.spec, scenario.page
    calls = Calls(monkeypatch)
    action = Action(monkeypatch, calls, spec.owner, spec.name, raised=FAULTS[fault])
    data = page.post(scenario.path, scenario.body, expect=500)
    assert len(action.ran) == 1
    # A product method outside the allow list did run on this session's objects.
    assert web_writes(action.records, page.ws), scenario.path
    assert_distrusted(page, data, scenario.saved, monkeypatch)


@pytest.mark.parametrize("scenario", sorted(ROUTES), indirect=True)
def test_i2_before_any_product_write_a_refusal_keeps_its_status(scenario, monkeypatch):
    """Every Write route: the route's reads ran (the allow-listed kinds, through the
    proxies), then a refusal was raised before any product write. It keeps its status,
    the session stays trusted, the tokens held before are unchanged and still honoured."""
    spec, page = scenario.spec, scenario.page
    calls = Calls(monkeypatch)
    action = Action(monkeypatch, calls, spec.owner, spec.name, replacement=refuse_after_reads)
    data = page.post(scenario.path, scenario.body, expect=409)
    assert data["error"]["code"] == "not_applicable" and data["ok"] is False
    assert action.ran == [] and any(name in READS[cls] for cls, name, _, _ in action.records)
    assert not web_writes(action.records, page.ws), scenario.path
    assert not page.ws.untrusted and held(page) == scenario.saved
    if scenario.card is not None:
        # The route's own function is restored first: on the confirm route it is the one
        # the business card's confirm below calls.
        monkeypatch.undo()
        tkey, body = scenario.card
        result = page.post(f"/api/device/tasks/{tkey}/confirm", body)["result"]
        assert result["status"] == "settled" and not page.ws.untrusted


@pytest.mark.parametrize("fault", list(FAULTS))
@pytest.mark.parametrize("scenario", QUIET, indirect=True)
def test_i2_a_route_without_a_product_write_keeps_a_refusal_raised_after_it(
    scenario, monkeypatch, fault
):
    """The routes whose positive path changes no product state of this session (the view
    and locale, the current reply, a plan's node selection, the preset on its isolated
    controller, the live switch, the lab's child): the service function ran, then a
    refusal or a contained result left ``action.run``. Nothing changed that a token could
    act on, so the answer is as before: the refusal keeps its status, the contained result
    stays contained, and the session stays trusted."""
    spec, page = scenario.spec, scenario.page
    calls = Calls(monkeypatch)
    action = Action(monkeypatch, calls, spec.owner, spec.name, raised=FAULTS[fault])
    expected = (409, "not_applicable") if fault == "409" else (500, "outcome_unknown")
    data = page.post(scenario.path, scenario.body, expect=expected[0])
    assert len(action.ran) == 1 and data["error"]["code"] == expected[1]
    assert not web_writes(action.records, page.ws), scenario.path
    assert not page.ws.untrusted


@pytest.mark.parametrize("scenario", sorted(ROUTES), indirect=True)
def test_i2_every_product_call_of_a_route_is_allow_listed_or_counted(scenario, monkeypatch):
    """The completeness of the allow list and the proxies (item 2): on each route's
    positive path, every product method the web layer calls is either on the allow list
    (a read, called as it is) or made through a proxy (counted); none is called directly
    by a web module outside the allow list. The route's classification above holds: a
    writing route makes at least one counted call, a quiet one makes none."""
    spec, page = scenario.spec, scenario.page
    calls = Calls(monkeypatch)
    action = Action(monkeypatch, calls, spec.owner, spec.name)
    page.post(scenario.path, scenario.body)
    records = list(calls.seen)
    assert records and len(action.ran) == 1
    assert strays(records) == []
    assert bool(web_writes(action.records, page.ws)) is spec.writes, scenario.path


def test_i2_reads_call_only_allow_listed_product_methods(browser, monkeypatch):
    """The GET routes make no product call outside the allow list, directly or counted."""
    rkey = browser.send(YELLOW)
    open_query(browser)
    calls = Calls(monkeypatch)
    for path in (
        "/api/state",
        "/api/i18n",
        "/api/tabs/accounts",
        "/api/tabs/wealth",
        "/api/tabs/plans",
        "/api/tabs/operator",
        f"/api/evidence/responses/{rkey}",
        "/api/live",
    ):
        browser.get(path)
    records = list(calls.seen)
    assert records and strays(records) == [] and not web_writes(records, browser.ws)


# --- RW-5 item 2: the allow list, the proxies and the count -----------------------------------


def test_i2_the_read_only_allow_list_is_the_reviewed_one():
    assert web_state.CONTROLLER_READS == READS["DemoController"]
    assert web_state.SESSION_READS == READS["DomesticSession"]
    assert web_state.WEALTH_READS == READS["WealthController"]
    for cls, names in (
        (DemoController, READS["DemoController"]),
        (DomesticSession, READS["DomesticSession"]),
    ):
        for name in names:
            assert inspect.isfunction(inspect.getattr_static(cls, name)), name


def test_i2_each_allow_listed_read_leaves_the_product_state_unchanged(browser):
    """Each entry of the allow list, called directly on the product objects with real
    arguments (a gated reply, a prepared red card, a purchase review, a registered plan),
    leaves a fingerprint of the product state unchanged: the snapshot (pendings, results,
    receipts, audit records, plans, schedules, positions), the drafts, the plans, the
    claimed requests, the id counter, the gate records, the replies and the history."""
    gate = browser.send(NEW)
    red = "T" + browser.send(RED)
    browser.device(red, "confirm")
    review = browser.send(PURCHASE)
    plan = "T" + browser.send(BIRTHDAY)
    ws = browser.ws
    c, s = target(ws.controller), target(ws.session)
    purchase = s.response(ws.rkeys[review])["payload"]["purchase"]

    def fingerprint():
        return (
            compat.H(c.snapshot()),
            sorted(c._drafts),
            sorted(c._plans),
            dict(c._request_facts),
            c._counter,
            deepcopy(c._scam_gate._entries),
            deepcopy(s._responses),
            list(s.history),
            s._sequence,
            s.tool_calls,
        )

    before = fingerprint()
    called = set()
    with bound(ws):
        for identity in (ws.tasks[gate_key].identity for gate_key in ("T" + gate, red, plan)):
            c.scam_gate_status(identity)
            c.kernel_refusal(identity)
            cards = c._authoritative_cards(identity)
            c._scam_grade(cards, NEW, 0)
            called |= {"scam_gate_status", "kernel_refusal", "_authoritative_cards", "_scam_grade"}
        c.snapshot()
        c.at_transaction_boundary()
        c._registered_plan_ids()
        c.risk_retake_blocked()
        called |= {
            "snapshot",
            "at_transaction_boundary",
            "_registered_plan_ids",
            "risk_retake_blocked",
        }
        s.response(ws.rkeys[review])
        s.aa_roster()
        s.purchase_review_terms(purchase["product_ref"], purchase["amount"], purchase["objective"])
        called |= {"response", "aa_roster", "purchase_review_terms"}
    assert called == READS["DemoController"] | READS["DomesticSession"]
    assert fingerprint() == before


def test_i2_the_proxy_counts_every_call_outside_the_allow_list():
    """``state.Product``: a field read passes through; an allow-listed method is returned
    as it is; any other method counts before it runs (a call that raises halfway counts);
    an attribute write or deletion counts; ``unwrap`` gives the object back; the count
    has no setter and nothing that clears it."""

    class Thing:
        field = 1

        def __init__(self):
            self.calls = []

        def read(self):
            self.calls.append("read")
            return "r"

        def write(self, value):
            self.calls.append(("write", value))
            return value

        def broken(self):
            self.calls.append("broken")
            raise RuntimeError("halfway")

    effects = web_state.Effects()
    thing = Thing()
    proxy = web_state.Product(thing, effects, frozenset({"read"}))
    assert proxy.field == 1 and proxy.read() == "r" and effects.count == 0
    assert proxy.write(2) == 2 and effects.count == 1
    with pytest.raises(RuntimeError):
        proxy.broken()
    assert effects.count == 2 and thing.calls == ["read", ("write", 2), "broken"]
    proxy.field = 3
    assert thing.field == 3 and effects.count == 3
    del proxy.field
    assert "field" not in vars(thing) and proxy.field == 1 and effects.count == 4
    assert web_state.unwrap(proxy) is thing and web_state.unwrap(thing) is thing
    with pytest.raises(AttributeError):
        effects.count = 0
    assert not any(name in dir(effects) for name in ("reset", "clear", "set"))
    with pytest.raises(AttributeError):
        proxy.missing


def test_i2_a_session_hands_out_only_proxies_sharing_one_count():
    """``WebSession``: the controller, the chat session and the wealth controller are all
    proxies over one count; the chat session holds the controller itself; building a
    session from proxies again wraps them once; reads count nothing, writes count."""
    ws = web_state.new_web_session("judge", "zh-CN", browser_id="b" * 43, tab_id="t" * 32)
    for name in ("controller", "session", "wealth"):
        held_object = getattr(ws, name)
        assert isinstance(held_object, web_state.Product), name
        assert held_object._effects is ws.effects, name
    assert isinstance(web_state.unwrap(ws.controller), DemoController)
    assert isinstance(web_state.unwrap(ws.session), DomesticSession)
    assert isinstance(web_state.unwrap(ws.wealth), WealthController)
    assert ws.session.controller is web_state.unwrap(ws.controller)
    assert ws.controller._readonly == web_state.CONTROLLER_READS
    assert ws.session._readonly == web_state.SESSION_READS
    assert ws.wealth._readonly == web_state.WEALTH_READS
    again = dataclasses.replace(ws)
    assert web_state.unwrap(again.controller) is web_state.unwrap(ws.controller)
    assert not isinstance(web_state.unwrap(again.controller), web_state.Product)
    count = ws.effects.count
    with bound(ws):
        ws.controller.snapshot()
        ws.controller.at_transaction_boundary()
        ws.session.aa_roster()
        assert ws.effects.count == count
        ws.controller.start_session()
        assert ws.effects.count == count + 1
        ws.wealth.start_session()
        assert ws.effects.count == count + 2
        ws.session.receive(BALANCE)
        assert ws.effects.count == count + 3


def test_i2_an_action_cannot_clear_the_count_before_its_refusal(browser, monkeypatch):
    """The handler holds the session's counter object and compares its count, which has
    no setter: an action that wrote, then tried to reset the count and replaced the
    session's counter before raising a refusal, is still answered as an unknown result."""
    open_query(browser)
    saved = held(browser)
    original = service.subscriptions_query
    noted, replaced = [], []

    def wrote_then_tampered(ws):
        original(ws)
        count = ws.effects.count
        try:
            ws.effects.count = 0
        except AttributeError:
            noted.append("no setter")
        assert ws.effects.count == count
        ws.effects = web_state.Effects()
        replaced.append(ws.effects)
        raise refusal()

    monkeypatch.setattr(service, "subscriptions_query", wrote_then_tampered)
    data = browser.post("/api/subscriptions/query", {}, expect=500)
    # The tampering ran to its end (the session holds the replaced counter now), and the
    # answer is still an unknown result: the handler compares the counter it took before.
    assert noted == ["no setter"] and saved
    assert replaced and browser.ws.effects is replaced[0] and replaced[0].count == 0
    assert_distrusted(browser, data, saved, monkeypatch)


def test_i2_a_finished_choice_chosen_again_is_refused_before_the_preparer_runs(
    browser, monkeypatch
):
    """The page's own record of a prepared chat choice (``service.CHOSEN``): the same
    choice again, after the card it prepared closed with an expected failure (its confirm
    raised one of the operation's ``FAILURES``; the listing is unchanged), is refused
    before the preparer is called at all, so the refusal is one that changed nothing: 409
    ``not_applicable``, the session trusted and still working (R2-E15, RW-5 item 2)."""
    tkey, card = open_query(browser)
    path, body = subscription_choice(browser)
    listing = path.split("/")[4]
    chosen = browser.post(path, body)["rkey"]

    def failed(ws, task, info):
        raise RuntimeError("an expected failure")

    monkeypatch.setattr(service, "_confirm_call", failed)
    data = browser.post(
        f"/api/device/tasks/T{chosen}/confirm", held_body(browser, "T" + chosen), expect=422
    )
    assert data["error"]["code"] == "operation_failed" and not browser.ws.untrusted
    monkeypatch.undo()
    assert browser.task("T" + chosen)["step"] == "closed_failed"
    browser.post(f"/api/chat/responses/{listing}/current", {})
    called = arm_after(monkeypatch, DomesticSession, "prepare_subscription_cancel", lambda: None)
    data = browser.fail(path, body, "not_applicable")
    assert called == [] and not browser.ws.untrusted and "state" in data
    assert browser.post(f"/api/device/tasks/{tkey}/confirm", card)["result"]["status"] == "settled"


# --- RW-5 item 3 c: the R6 review's follow-ups --------------------------------------------------


def birthday_registered_then_refused(page, monkeypatch, closing, raised):
    """choose_birthday: the preparer returned the draft (a plan is registered), then the
    page's registration of its task raised."""
    path, body = birthday_choice(page)
    c = target(page.ws.controller)
    plans = len(c._plans)
    original = service._business

    def registered_then_raised(*args, **kwargs):
        original(*args, **kwargs)
        closing.arm()
        raise raised()

    monkeypatch.setattr(service, "_business", registered_then_raised)
    data = page.post(path, body, expect=500)
    assert len(c._plans) == plans + 1
    return data


def compensation_recorded_then_refused(page, monkeypatch, closing, raised):
    """compensate_latest: the gateway audited the request and the controller prepared the
    unlock card, then the page's registration of the card's task raised."""
    version = page.tab("operator")["compensation"]["compensate_version"]
    c = target(page.ws.controller)
    pending = set(c._pending)
    original = service._business

    def registered_then_raised(*args, **kwargs):
        original(*args, **kwargs)
        closing.arm()
        raise raised()

    monkeypatch.setattr(service, "_business", registered_then_raised)
    data = page.post("/api/chat/compensate", {"plan_version": version}, expect=500)
    (prepared,) = set(c._pending) - pending
    assert c._pending[prepared]["compensation_plan_id"]
    return data


def query_recorded_then_refused(page, monkeypatch, closing, raised):
    """An ordinary business page action: the controller prepared the query's card, then
    the page's registration of the card's task raised."""
    c = target(page.ws.controller)
    pending = set(c._pending)
    original = service._business

    def registered_then_raised(*args, **kwargs):
        original(*args, **kwargs)
        closing.arm()
        raise raised()

    monkeypatch.setattr(service, "_business", registered_then_raised)
    data = page.post("/api/subscriptions/query", {}, expect=500)
    assert set(c._pending) - pending
    return data


def birthday_control(page):
    path, body = birthday_choice(page)
    page.post(path, body)
    assert "birthday_draft" in page.ws.business


def compensation_control(page):
    version = page.tab("operator")["compensation"]["compensate_version"]
    page.post("/api/chat/compensate", {"plan_version": version})
    assert any(task.source == "compensation" for task in page.ws.tasks.values())


def query_control(page):
    page.post("/api/subscriptions/query", {})
    assert any(task.source == "subscriptions" for task in page.ws.tasks.values())


def plan_dispatched(page):
    """A birthday plan signed and dispatched, its funds locked (what Compensate acts on).
    After the red card: a debit reserved after the lock would consume the lock, and the
    chat Stop of ``dispatched_plan`` would retire the red card; neither is needed."""
    plan = "T" + page.send(BIRTHDAY)
    version = page.task(plan)["nodes_version"]
    nodes = {"card_version": version, "nodes": ["n0", "n1", "n2", "n3"]}
    page.post(f"/api/device/tasks/{plan}/nodes", nodes)
    page.device(plan, "confirm")
    assert page.task(plan)["step"] == "done"


# The three classes of call: what follows the red card, the double fault, the control.
DOUBLE = {
    "birthday": (None, birthday_registered_then_refused, birthday_control),
    "compensate": (plan_dispatched, compensation_recorded_then_refused, compensation_control),
    "subscriptions-query": (None, query_recorded_then_refused, query_control),
}
# The closing faults of R5 after a call that ran: a refusal or a contained result from the
# refresh or from the live sync.
RAN_CLOSING = ["refresh-409", "refresh-contained", "sync-422", "sync-contained"]


def at_mfa_request(page, then):
    """The scenario's starting point (item 3 c): a gated reply (a gate record) and a red
    business card at ``mfa_request`` with a valid token, the tokens saved; ``then`` is
    what the call needs besides (the plan Compensate acts on)."""
    gated(page)
    red, body = red_business_card(page)
    if then is not None:
        then(page)
    saved = held(page)
    assert red in saved and page.ws.gates and page.sms_code() is None
    assert page.task(red)["step"] == "mfa_request" and held_body(page, red) == body
    return red, body, saved


@pytest.mark.parametrize("closing_fault", RAN_CLOSING)
@pytest.mark.parametrize("fault", list(FAULTS))
@pytest.mark.parametrize("call", list(DOUBLE))
def test_i3_a_double_fault_from_a_valid_token_at_mfa_request_sends_no_code(
    browser, monkeypatch, call, fault, closing_fault
):
    """Item 3 c, follow-up 1 (with items 1 and 2): a red card waits at ``mfa_request`` with
    a valid token and the session holds a gate record. The call changed state, the
    recording after it raised a refusal or a contained result (item 1, item 2), and the
    closing refresh or live sync raised too. An unknown result: the session is distrusted,
    every gate record is gone (follow-up 2), the old token sends no code (``request_mfa``
    0, no live code on the phone)."""
    then, scenario, _ = DOUBLE[call]
    red, body, saved = at_mfa_request(browser, then)
    closing = Closing(monkeypatch, *CLOSING[closing_fault])
    data = scenario(browser, monkeypatch, closing, FAULTS[fault])
    closing.disarm()
    assert_distrusted(browser, data, saved, monkeypatch)
    counts = spied(monkeypatch, *COUNTERS)
    browser.fail(f"/api/device/tasks/{red}/mfa/request", body, "not_applicable")
    assert counts == Counter() and browser.sms_code() is None


@pytest.mark.parametrize("call", list(DOUBLE))
def test_i3_the_same_token_sends_a_code_when_nothing_fails(browser, monkeypatch, call):
    """The control of follow-up 1: the same starting point, the same call without a fault.
    The red card's token is unchanged afterwards and sends a code: ``request_mfa`` 1, the
    code on the phone, the next step the code's entry."""
    then, _, control = DOUBLE[call]
    red, body, saved = at_mfa_request(browser, then)
    control(browser)
    assert held_body(browser, red) == body and not browser.ws.untrusted
    counts = spied(monkeypatch, *COUNTERS)
    data = browser.post(f"/api/device/tasks/{red}/mfa/request", body)
    assert data["result"] == {"status": "waiting_mfa"}
    assert counts == Counter({"request_mfa": 1})
    assert browser.sms_code() is not None
    assert browser.task(red)["gesture"]["op"] == "mfa_verify"


@pytest.mark.parametrize("name", COUNTERS)
def test_i3_each_spied_entry_point_counts_a_positive_calibration(browser, monkeypatch, name):
    """Item 3 c, follow-up 3: each of the six entry points ``spied`` watches counts the
    one call that reaches it, through the same wrapper ``assert_distrusted`` relies on: a
    zero there is a call that did not happen."""
    if name in ("approve", "confirm_input", "dispatch"):
        tkey = "T" + browser.send(YELLOW)
        body = held_body(browser, tkey)
        counts = spied(monkeypatch, *COUNTERS)
        assert (
            browser.post(f"/api/device/tasks/{tkey}/confirm", body)["result"]["status"] == "settled"
        )
        assert counts["approve"] == 1 and counts["dispatch"] == 1 and counts["confirm_input"] >= 1
        return
    tkey = "T" + browser.send(RED)
    browser.device(tkey, "confirm")
    if name == "request_mfa":
        counts = spied(monkeypatch, *COUNTERS)
        browser.device(tkey, "mfa_request")
        assert counts == Counter({"request_mfa": 1})
        return
    browser.device(tkey, "mfa_request")
    if name == "submit_mfa":
        counts = spied(monkeypatch, *COUNTERS)
        browser.device(tkey, "mfa_verify", code=browser.sms_code())
        assert counts == Counter({"submit_mfa": 1})
        return
    assert name == "complete_mfa"
    # No web route lets the device fill a code in (INV-2.2): the controller's own
    # device-filled path is called directly, as the R5 calibration does.
    identity = browser.ws.tasks[tkey].identity
    counts = spied(monkeypatch, *COUNTERS)
    with bound(browser.ws):
        assert target(browser.ws.controller).complete_mfa(identity)["status"] == "mfa_verified"
    assert counts["complete_mfa"] == 1
