"""Regression tests for the hardening that followed the R7 review.

Each test names the item of the hardening list it closes:

- ``test_h1_*`` (item 1): the expected failure that ``live.operation`` raises carries a
  source mark bound to its gesture and session. A ``WebOperationFailed`` without that mark
  (or marked by another session's gesture) raised after a product call that may have
  changed state is an unknown result and the session is distrusted; raised before any
  such call, it keeps its 422. A marked expected failure keeps its 422 and the session
  stays trusted, also after the product call it followed.
- ``test_h2_*`` (item 2): the count lives in a closure that ordinary code cannot reset;
  the three proxies are bound to it when the session is built; before it judges, the
  handler checks that the counter it took is still the session's and the proxies' own.
  A count reset inside a request, and a counter or a proxy replaced in one request and
  relied on in the next, are answered as unknown results.

Item 3 (the reviewed list of sub-object uses) is in ``test_web_guards.py``; item 4 makes
``assert_distrusted`` of R5 and R6 assert the 409 and its code at each call. The real
handlers, service functions, controllers, gateway and kernel run throughout, except where
a test injects the fault it names.
"""

import contextlib
import copy
import pickle

import pytest

from corridor.agent.domestic_session import DomesticSession
from corridor.ui.controller import DemoController
from corridor.web import errors, live, service
from corridor.web import state as web_state
from corridor.web.executor import bound
from tests.web.test_api_chat import Browser
from tests.web.test_review_r1 import held_body
from tests.web.test_review_r5 import held, raise_after
from tests.web.test_review_r6 import (
    FAULTS,
    READS,
    ROUTES,
    WRITING,
    Action,
    Calls,
    Scenario,
    assert_distrusted,
    red_business_card,
    target,
    web_writes,
)
from tests.web.test_tasks import YELLOW


@pytest.fixture
def browser(web_app):
    page = Browser(web_app)
    page.login()
    return page


@pytest.fixture
def scenario(request, monkeypatch, tmp_path):
    built = Scenario(request.param, monkeypatch, tmp_path)
    try:
        yield built
    finally:
        built.close()


def other_session():
    return web_state.new_web_session("judge", "zh-CN", browser_id="o" * 43, tab_id="o" * 32)


def foreign_failure():
    """An expected failure raised by a gesture of another session: marked, but not for
    the session whose request it leaves."""
    other = other_session()
    try:
        with bound(other), live.operation(other, "domestic"):
            raise RuntimeError("expected")
    except errors.WebOperationFailed as failure:
        return failure
    raise AssertionError("live.operation raised no expected failure")


# What is injected as an unmarked expected failure: the class itself (``error`` builds
# the same), and one marked by another session's gesture.
UNMARKED = {"unmarked": errors.WebOperationFailed, "foreign": foreign_failure}


# --- item 1: the source mark ------------------------------------------------------------------


def test_h1_the_operations_expected_failure_carries_its_gesture_as_source_mark():
    """``live.operation``: the expected failure's ``origin`` is the gesture, which holds
    that failure and its session; ``expected_failure`` accepts it for that session only,
    and refuses an unmarked failure, one built by ``error``, a copy marked with the same
    gesture, and one marked with anything else."""
    ws = other_session()
    with bound(ws):
        with pytest.raises(errors.WebOperationFailed) as raised:
            with live.operation(ws, "domestic") as op:
                raise RuntimeError("expected")
    failure = raised.value
    assert failure.origin is op and op.failure is failure and op.ws is ws
    assert live.expected_failure(failure, ws) is True
    assert live.expected_failure(failure, other_session()) is False
    assert live.expected_failure(errors.WebOperationFailed(), ws) is False
    assert live.expected_failure(errors.error("operation_failed"), ws) is False
    assert errors.error("operation_failed").origin is None
    assert live.expected_failure(errors.WebOperationFailed(origin=op), ws) is False
    assert live.expected_failure(errors.WebOperationFailed(origin=object()), ws) is False
    assert live.expected_failure(errors.WebOutcomeUnknown(), ws) is False
    assert live.expected_failure(RuntimeError(), ws) is False
    # A gesture that succeeded holds no failure.
    with bound(ws), live.operation(ws, "domestic") as done:
        pass
    assert done.failure is None and done.ws is ws


# Where a marked expected failure is raised after a product call that changed state: the
# product method that runs and then raises one of the operation's ``FAILURES``.
MARKED = {
    "chat-message": (DomesticSession, "receive"),
    "subscriptions-query": (DemoController, "preview_subscriptions"),
}


@pytest.mark.parametrize("route", list(MARKED))
def test_h1_a_marked_expected_failure_after_a_product_write_keeps_422_and_trust(
    browser, monkeypatch, route
):
    """A real expected failure: the product call ran (the count moved), then raised one
    of ``FAILURES`` inside ``live.operation``. It keeps 422 ``operation_failed``, the
    session stays trusted, and a red card's token held before still sends a code."""
    red, _ = red_business_card(browser)
    owner, name = MARKED[route]
    ran = raise_after(monkeypatch, owner, name, lambda: RuntimeError("expected"))
    before = browser.ws.effects.count
    if route == "chat-message":
        body = {"text": YELLOW, "external_files": 0, "client_msg_id": "rw6-marked-0001"}
        data = browser.post("/api/chat/message", body, expect=422)
    else:
        data = browser.post("/api/subscriptions/query", {}, expect=422)
    assert data["ok"] is False and data["error"]["code"] == "operation_failed"
    assert len(ran) == 1 and browser.ws.effects.count > before
    assert browser.ws.untrusted is False
    monkeypatch.undo()
    browser.device(red, "mfa_request")
    assert browser.sms_code() is not None and browser.ws.untrusted is False


def test_h1_a_marked_expected_failure_of_a_device_call_keeps_422_and_trust(browser, monkeypatch):
    """The device's confirm ran on a yellow card (the count moved), then raised one of
    ``FAILURES`` inside the operation: 422, the session stays trusted, and a red card's
    token held before still sends a code."""
    red, _ = red_business_card(browser)
    yellow = "T" + browser.send(YELLOW)
    original = service._confirm_call
    ran = []

    def confirmed_then_failed(ws, task, info):
        ran.append(original(ws, task, info))
        raise RuntimeError("expected")

    monkeypatch.setattr(service, "_confirm_call", confirmed_then_failed)
    before = browser.ws.effects.count
    data = browser.post(
        f"/api/device/tasks/{yellow}/confirm", held_body(browser, yellow), expect=422
    )
    assert data["error"]["code"] == "operation_failed" and len(ran) == 1
    assert browser.ws.effects.count > before and browser.ws.untrusted is False
    monkeypatch.undo()
    browser.device(red, "mfa_request")
    assert browser.sms_code() is not None and browser.ws.untrusted is False


@pytest.mark.parametrize("kind", ["unmarked"])
@pytest.mark.parametrize("scenario", WRITING, indirect=True)
def test_h1_an_unmarked_expected_failure_after_the_first_product_write_is_unknown(
    scenario, monkeypatch, kind
):
    """Every Write route that calls a product method outside the allow list: the route's
    service function ran as it is, then a ``WebOperationFailed`` without the source mark
    left ``action.run``. An unknown result (500), the session distrusted, no token held
    before honoured again (R7 blocker 1)."""
    spec, page = scenario.spec, scenario.page
    calls = Calls(monkeypatch)
    action = Action(monkeypatch, calls, spec.owner, spec.name, raised=UNMARKED[kind])
    data = page.post(scenario.path, scenario.body, expect=500)
    assert len(action.ran) == 1
    assert web_writes(action.records, page.ws), scenario.path
    assert_distrusted(page, data, scenario.saved, monkeypatch)


FOREIGN_ROUTES = ["/api/chat/message", "/api/subscriptions/query", "/api/wealth/purchase"]


@pytest.mark.parametrize("scenario", FOREIGN_ROUTES, indirect=True)
def test_h1_an_expected_failure_marked_by_another_session_after_a_write_is_unknown(
    scenario, monkeypatch
):
    """The mark binds the failure to its own gesture's session: one raised by another
    session's gesture is unmarked here, so after a product write it is unknown."""
    spec, page = scenario.spec, scenario.page
    calls = Calls(monkeypatch)
    action = Action(monkeypatch, calls, spec.owner, spec.name, raised=UNMARKED["foreign"])
    data = page.post(scenario.path, scenario.body, expect=500)
    assert len(action.ran) == 1 and web_writes(action.records, page.ws)
    assert_distrusted(page, data, scenario.saved, monkeypatch)


class FailingSent(dict):
    """``ws.sent`` that stores the entry, then raises the given failure once armed: the
    step after ``service._submit``'s operation and its ``_unknown`` (R7 blocker 1)."""

    def __init__(self, items, raised):
        super().__init__(items)
        self.raised = raised
        self.hits = 0

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        self.hits += 1
        raise self.raised()

    def popitem(self, last=True):
        return super().popitem()


@pytest.mark.parametrize("kind", list(UNMARKED))
def test_h1_an_unmarked_expected_failure_after_the_reply_is_recorded_is_unknown(
    browser, monkeypatch, kind
):
    """``/api/chat/message``: the reply was received (counted), recorded, the current reply
    set; storing the message id then raised an unmarked ``WebOperationFailed``. An
    unknown result, the session distrusted, the red card's token refused."""
    red, _ = red_business_card(browser)
    saved = held(browser)
    history = len(target(browser.ws.session).history)
    browser.ws.sent = failing = FailingSent(browser.ws.sent, UNMARKED[kind])
    body = {"text": YELLOW, "external_files": 0, "client_msg_id": "rw6-sent-0001"}
    data = browser.post("/api/chat/message", body, expect=500)
    assert failing.hits == 1 and len(target(browser.ws.session).history) == history + 1
    assert red in saved
    assert_distrusted(browser, data, saved, monkeypatch)
    assert browser.sms_code() is None


def refuse_unmarked_after_reads(ws, *args, **kwargs):
    """The reads of every allow-listed kind, then an unmarked ``WebOperationFailed``."""
    c = ws.controller
    c.snapshot()
    c.at_transaction_boundary()
    c._registered_plan_ids()
    ws.session.aa_roster()
    raise errors.WebOperationFailed()


@pytest.mark.parametrize("scenario", sorted(ROUTES), indirect=True)
def test_h1_an_unmarked_expected_failure_before_any_product_write_keeps_422(scenario, monkeypatch):
    """Every Write route: only reads ran, then an unmarked ``WebOperationFailed`` was
    raised. Nothing changed, so it keeps 422 ``operation_failed``, the session stays
    trusted and the tokens held before are unchanged."""
    spec, page = scenario.spec, scenario.page
    calls = Calls(monkeypatch)
    action = Action(
        monkeypatch, calls, spec.owner, spec.name, replacement=refuse_unmarked_after_reads
    )
    data = page.post(scenario.path, scenario.body, expect=422)
    assert data["ok"] is False and data["error"]["code"] == "operation_failed"
    assert action.ran == [] and any(name in READS[cls] for cls, name, _, _ in action.records)
    assert not web_writes(action.records, page.ws), scenario.path
    assert page.ws.untrusted is False and held(page) == scenario.saved


# --- item 2: the counter and its binding ---------------------------------------------------


def test_h2_ordinary_code_cannot_reset_or_copy_the_count():
    """``state.Effects``: the number lives in a closure. No field to write back, no
    attribute write or deletion, ``__init__`` again changes nothing, no copy or pickle, no
    ``__dict__``; a new counter starts at 0 and leaves the first one as it was."""
    effects = web_state.Effects()
    effects.touch()
    effects.touch()
    attempts = {
        "slot": lambda: setattr(effects, "_count", 0),
        "property": lambda: setattr(effects, "count", 0),
        "reader": lambda: setattr(effects, "_read", lambda: 0),
        "bumper": lambda: setattr(effects, "_bump", lambda: None),
        "delete": lambda: delattr(effects, "_read"),
        "vars": lambda: vars(effects),
    }
    for name, attempt in attempts.items():
        with pytest.raises((AttributeError, TypeError)):
            attempt()
        assert effects.count == 2, name
    effects.__init__()
    assert effects.count == 2
    for duplicate in (copy.copy, copy.deepcopy, pickle.dumps):
        with pytest.raises(TypeError):
            duplicate(effects)
    fresh = web_state.Effects.__new__(web_state.Effects)
    assert fresh.count == 0 and effects.count == 2
    effects.touch()
    assert effects.count == 3 and fresh.count == 0 and repr(effects) == "Effects(3)"


def test_h2_the_session_binds_its_proxies_to_its_counter_and_the_check_sees_any_change():
    """``effects_bound``: true for a session as built; false once the counter, or any of
    the three proxies, is replaced or unwrapped; a session cannot be built around anything
    but an ``Effects``."""
    ws = other_session()
    effects = ws.effects
    assert web_state.effects_bound(ws, effects) is True
    assert web_state.PRODUCTS == PRODUCTS
    for name in web_state.PRODUCTS:
        assert web_state.bound_effects(getattr(ws, name)) is effects
    assert web_state.bound_effects(web_state.unwrap(ws.controller)) is None
    assert web_state.effects_bound(ws, web_state.Effects()) is False

    def replaced(change):
        probe = other_session()
        change(probe)
        return web_state.effects_bound(probe, probe.effects)

    assert replaced(lambda s: setattr(s, "effects", web_state.Effects())) is False
    for name, reads in (
        ("controller", web_state.CONTROLLER_READS),
        ("session", web_state.SESSION_READS),
        ("wealth", web_state.WEALTH_READS),
    ):
        assert replaced(lambda s, n=name: setattr(s, n, web_state.unwrap(getattr(s, n)))) is False
        assert (
            replaced(
                lambda s, n=name, r=reads: setattr(
                    s, n, web_state.Product(web_state.unwrap(getattr(s, n)), web_state.Effects(), r)
                )
            )
            is False
        )

    class Counting:
        count = 0

        def touch(self):
            self.count += 1

    with pytest.raises(TypeError):
        web_state.WebSession(
            browser_id="x" * 43,
            tab_id="x" * 32,
            generation="g",
            secret=b"s",
            view="judge",
            locale="zh-CN",
            controller=object(),
            wealth=object(),
            session=object(),
            pair="p",
            effects=Counting(),
        )


def reset_slot(ws, start):
    with contextlib.suppress(AttributeError, TypeError):
        ws.effects._count = start


def reset_init(ws, start):
    # ``__init__`` again, then the count brought back to where it was before the action.
    ws.effects.__init__()
    for _ in range(start):
        ws.effects.touch()


RESETS = {"slot": reset_slot, "init": reset_init}
REFUSALS = {**FAULTS, "unmarked": errors.WebOperationFailed}


@pytest.mark.parametrize("fault", list(REFUSALS))
@pytest.mark.parametrize("reset", list(RESETS))
def test_h2_a_count_reset_inside_the_action_does_not_hide_its_write(
    browser, monkeypatch, reset, fault
):
    """``/api/subscriptions/query``: the service function ran (its product calls counted),
    then the action tried to bring the count back to where it was (writing the old slot,
    or ``__init__`` and touches up to the old value) and raised a refusal, a contained
    result or an unmarked expected failure. The count was not reset: an unknown result."""
    red, _ = red_business_card(browser)
    saved = held(browser)
    original = service.subscriptions_query
    ran = []

    def wrote_then_reset(ws):
        start = ws.effects.count
        ran.append(original(ws))
        RESETS[reset](ws, start)
        raise REFUSALS[fault]()

    monkeypatch.setattr(service, "subscriptions_query", wrote_then_reset)
    data = browser.post("/api/subscriptions/query", {}, expect=500)
    assert len(ran) == 1 and red in saved
    assert_distrusted(browser, data, saved, monkeypatch)


def replace_counter(ws):
    ws.effects = web_state.Effects()


def replace_proxies(ws):
    other = web_state.Effects()
    ws.controller = web_state.Product(
        web_state.unwrap(ws.controller), other, web_state.CONTROLLER_READS
    )
    ws.session = web_state.Product(web_state.unwrap(ws.session), other, web_state.SESSION_READS)
    ws.wealth = web_state.Product(web_state.unwrap(ws.wealth), other, web_state.WEALTH_READS)


def unwrap_products(ws):
    for name in PRODUCTS:
        setattr(ws, name, web_state.unwrap(getattr(ws, name)))


# The session fields that hold product objects (``state.PRODUCTS``, written out so the
# request-level tests run unchanged on the code before RW-6).
PRODUCTS = ("controller", "session", "wealth")
REPLACEMENTS = {"counter": replace_counter, "proxies": replace_proxies, "raw": unwrap_products}


@pytest.mark.parametrize("fault", list(FAULTS))
@pytest.mark.parametrize("when", ["after-write", "before-write"])
@pytest.mark.parametrize("replacement", list(REPLACEMENTS))
def test_h2_a_binding_replaced_in_one_request_fails_closed_in_the_next(
    browser, monkeypatch, replacement, when, fault
):
    """One request's action replaced the session's counter, or its proxies with ones
    bound to another counter, or the proxies with the bare product objects, and returned
    normally. In the next request, a refusal or a contained result raised after the
    route's product calls (whose count the session's counter no longer sees), or before
    any, is an unknown result: the handler judges by the count only while the binding
    made when the session was built still holds (fail closed)."""
    red, _ = red_business_card(browser)
    original_view = service.set_view

    def viewed_then_replaced(ws, *args, **kwargs):
        result = original_view(ws, *args, **kwargs)
        REPLACEMENTS[replacement](ws)
        return result

    monkeypatch.setattr(service, "set_view", viewed_then_replaced)
    browser.post("/api/session/view", {"view": "judge"})
    # The binding no longer holds: some product field is not a proxy counting into the
    # session's counter (read without ``effects_bound``, so the check is the same before
    # and after the RW-6 change).
    ws = browser.ws
    assert any(
        not isinstance(getattr(ws, name), web_state.Product)
        or object.__getattribute__(getattr(ws, name), "_effects") is not ws.effects
        for name in PRODUCTS
    )
    assert browser.ws.untrusted is False
    saved = held(browser)
    assert red in saved
    original_query = service.subscriptions_query
    ran = []

    def queried_then_refused(ws):
        if when == "after-write":
            ran.append(original_query(ws))
        raise FAULTS[fault]()

    monkeypatch.setattr(service, "subscriptions_query", queried_then_refused)
    data = browser.post("/api/subscriptions/query", {}, expect=500)
    assert len(ran) == (1 if when == "after-write" else 0)
    assert_distrusted(browser, data, saved, monkeypatch)
