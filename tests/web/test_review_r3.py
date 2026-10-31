"""Regression tests for the third review round (R3) of the web front end.

Each test names the item it closes (the numbering of WEB-R2-ADOPTION.md, R3 section):
RW-4'' item 1 (the authorization lab is closed in the submission build), E-3, E-4, E-10,
E-14 and E-15. The real handlers, service functions, controllers, gateway and kernel run
throughout; they are only spied on, except where a test injects the fault or the state
change the review described. This module runs the submission build: ``FEATURES`` as
shipped, nothing opened on top.
"""

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

import corridor.web as web_package
from corridor.agent.domestic_session import DomesticSession
from corridor.bench import investment_runner
from corridor.gateway.service import Gateway
from corridor.kernel.binding import GREEN
from corridor.sim.device import Device
from corridor.ui.controller import DemoController
from corridor.ui.live_config import DEPLOYMENTS
from corridor.ui.live_runtime import access_proof
from corridor.web import compat, handlers, lab, purchase, service, tasks
from corridor.web import state as web_state
from corridor.web.executor import bound
from tests.suggest_fakes import TRANSFER, UNCLEAR, suggest_rule
from tests.unit.test_ui_live_mode import ACCESS_CODE, FakeModel, environ
from tests.unit.test_ui_scam_friction import CLARIFY_PLAIN, NEW, SAFE
from tests.web.conftest import WebClient
from tests.web.test_api_chat import Browser
from tests.web.test_review_r1 import finish, held_body, newest, spied
from tests.web.test_tasks import BIRTHDAY, RED, YELLOW, logged_in

PURCHASE = "帮我申购2000元稳健30天持有"
MONTHLY = "每月5号给房东转500元"
CANCEL_MONTHLY = "取消每月5号给房东的转账"
STALE = {"X-Corridor-Generation": "x" * 12}
STAGE_JS = Path(web_package.__file__).with_name("static") / "js" / "views" / "stage.js"
LAB_POSTS = {
    "/api/lab/start": {"scenario": "yellow", "mode": "scripted"},
    "/api/lab/change": {"lab_generation": "x" * 12},
    "/api/lab/try-previous": {"lab_generation": "x" * 12},
    "/api/lab/gesture": {
        "lab_generation": "x" * 12,
        "tkey": "TR1",
        "op": "confirm",
        "gesture": "g" * 22,
        "card_version": "0" * 32,
    },
}
AA = "/api/chat/responses/{rkey}/aa"


@pytest.fixture
def browser(web_app):
    page = Browser(web_app)
    page.login()
    return page


def business(page, source):
    return [task for task in page.state["device"]["tasks"] if task["source"] == source]


def executable(page, source):
    return [task for task in business(page, source) if task["gesture"] is not None]


def open_query(page):
    """A business card with a live token that no chat reply owns."""
    page.post("/api/subscriptions/query", {})
    (task,) = business(page, "subscriptions")
    assert task["gesture"] is not None
    return task["key"], held_body(page, task["key"])


def dispatched_plan(page):
    """A birthday plan signed and stopped: the task panel's Compensate acts on it."""
    plan = "T" + page.send(BIRTHDAY)
    page.post(
        f"/api/device/tasks/{plan}/nodes",
        {"card_version": page.task(plan)["nodes_version"], "nodes": ["n0", "n1", "n2", "n3"]},
    )
    page.device(plan, "confirm")
    page.send("停止")


# --- RW-4'' item 1: the authorization lab is closed in the submission build ----------------


def test_rw4_the_submission_build_closes_every_lab_route(web_app, monkeypatch):
    """No ``lab`` in FEATURES, ``capabilities.lab`` false in both views, and every lab
    route answers 404 before any lab code runs, logged in or not, whatever its body or
    generation (RW-4'''; ``test_review_r4.py`` proves the order): no child session is
    built and no lab pairing joins the phone allow list."""
    assert lab.FEATURE not in web_package.FEATURES
    assert web_app.settings["features"] is None
    reached = []
    for name in ("open_to", "view", "start", "change", "try_previous", "gesture"):
        monkeypatch.setattr(lab, name, lambda *a, _name=name, **k: reached.append(_name))
    pages = {view: Browser(web_app, view=view) for view in ("customer", "judge")}
    built = []
    original = web_state.new_web_session

    def counted(*args, **kwargs):
        built.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(web_state, "new_web_session", counted)
    for page in pages.values():
        for logged_in_now in (False, True):
            if logged_in_now:
                page.login()
            assert page.state["capabilities"]["lab"] is False
            data = page.get("/api/lab", expect=404)
            assert data["error"]["code"] == "not_found" and "lab" not in data
            for path, body in LAB_POSTS.items():
                data = page.post(path, body, expect=404)
                assert data["error"]["code"] == "not_found" and "lab" not in data
                assert page.client.post(path, {**body, "extra": 1}).code == 404
                assert page.client.post(path, body, headers=STALE).code == 404
            assert page.client.get("/api/lab/phone").code == 404
    anonymous = WebClient(web_app)
    for path, body in LAB_POSTS.items():
        assert anonymous.post(path, body).code == 404
    assert reached == [] and built == []
    for page in pages.values():
        assert page.ws.demos == {}
    # Only the two pages' own pairings are on the phone allow list.
    allowed = web_app.settings["store"].pairs._allowed
    assert allowed == {page.pair() for page in pages.values()}
    # The page draws the entry only for the judge view with the capability open.
    gate = 'state.view === "judge" && Boolean(state.capabilities && state.capabilities.lab)'
    assert gate in STAGE_JS.read_text(encoding="utf-8")


# --- E-3: a failure inside the birthday or the compensation preparer -----------------------


@pytest.mark.parametrize("failure", [RuntimeError, ValueError, TypeError])
def test_e3_a_failure_inside_the_birthday_preparer_distrusts_the_session(
    browser, monkeypatch, failure
):
    tkey, saved = open_query(browser)
    rkey = browser.send("帮我准备生日")
    assert browser.task(tkey)["gesture"] is not None
    choice = next(
        item["key"] for item in browser.response(rkey)["birthday"]["choices"] if item["enabled"]
    )
    original = DemoController.preview_birthday

    def prepared_then_failed(self, *args, **kwargs):
        original(self, *args, **kwargs)
        raise failure("after the request was recorded and the plan registered")

    monkeypatch.setattr(DemoController, "preview_birthday", prepared_then_failed)
    data = browser.post(f"/api/chat/responses/{rkey}/birthday", {"choice": choice}, expect=500)
    assert data["error"]["code"] == "outcome_unknown" and browser.ws.untrusted
    assert browser.ws.gestures == {} and "birthday_choice_error" not in browser.ws.business
    assert all(task["gesture"] is None for task in data["state"]["device"]["tasks"])
    counts = spied(monkeypatch, "approve", "dispatch", "request_mfa", "submit_mfa")
    browser.fail(f"/api/device/tasks/{tkey}/confirm", saved, "not_applicable")
    assert counts == Counter()


def test_e3_a_birthday_result_without_a_draft_stays_an_ordinary_page_error(browser, monkeypatch):
    rkey = browser.send("帮我准备生日")
    choice = next(
        item["key"] for item in browser.response(rkey)["birthday"]["choices"] if item["enabled"]
    )
    # Returned, not raised, and nothing recorded: a known refusal.
    monkeypatch.setattr(
        DemoController, "preview_birthday", lambda self, **kwargs: {"status": "outside_lock_month"}
    )
    browser.post(f"/api/chat/responses/{rkey}/birthday", {"choice": choice})
    assert browser.ws.business["birthday_choice_error"] == rkey
    assert not browser.ws.untrusted


@pytest.mark.parametrize("failure", [RuntimeError, KeyError, TypeError])
def test_e3_a_failure_inside_compensation_distrusts_the_session(browser, monkeypatch, failure):
    dispatched_plan(browser)
    tkey, saved = open_query(browser)
    original = DemoController.compensate

    def prepared_then_failed(self, plan_id):
        original(self, plan_id)
        raise failure("after the request was audited and the unlock card prepared")

    monkeypatch.setattr(DemoController, "compensate", prepared_then_failed)
    version = browser.tab("operator")["compensation"]["compensate_version"]
    data = browser.post("/api/chat/compensate", {"plan_version": version}, expect=500)
    assert data["error"]["code"] == "outcome_unknown" and browser.ws.untrusted
    assert browser.ws.gestures == {} and "control_error" not in browser.ws.business
    assert all(task["gesture"] is None for task in data["state"]["device"]["tasks"])
    counts = spied(monkeypatch, "approve", "dispatch", "request_mfa", "submit_mfa")
    browser.fail(f"/api/device/tasks/{tkey}/confirm", saved, "not_applicable")
    assert counts == Counter()


# --- E-4: the account a purchase's signed preference names --------------------------------


def test_e4_a_signed_preference_naming_another_account_is_withdrawn(browser, monkeypatch):
    """The device signs a preference whose source account is not the reviewed one. The
    product layer's own read-back (``DomesticSession._signed_terms``, not replaced here)
    finds it and withdraws the pending before any MFA, approval or dispatch, keeping and
    recording the signed input. The web answers that withdrawal as an unknown outcome,
    more strictly than needed: the session is distrusted."""
    rkey = browser.send(PURCHASE)
    tkey = "T" + rkey
    rid = browser.ws.rkeys[rkey]
    assert browser.task(tkey)["step"] == "review_purchase"
    reviewed = browser.ws.session.response(rid)["payload"]["purchase_terms"]
    assert reviewed["source_account_ref"] == "A_CURRENT"
    original = investment_runner._preference

    def another_account(case, changes=None):
        preference = original(case, changes)
        if case.identity == "DEMO:purchase-terms":
            return preference  # the review's own unsigned copy
        return preference.model_copy(update={"source_account_ref": "A_LOCKED"})

    monkeypatch.setattr(investment_runner, "_preference", another_account)
    c = browser.ws.controller
    before = set(c.state.user_inputs)
    counts = spied(monkeypatch, "confirm_input", "request_mfa", "submit_mfa", "approve", "dispatch")
    data = browser.post(f"/api/device/tasks/{tkey}/confirm", held_body(browser, tkey), expect=500)
    assert data["error"]["code"] == "outcome_unknown" and browser.ws.untrusted
    raw = browser.ws.session.response(rid)
    withdrawn = raw["payload"]["withdrawn_input"]
    assert raw["status"] == "clarify" and withdrawn["reason"] == "differs"
    assert withdrawn["preference_ref"] in set(c.state.user_inputs) - before
    assert (
        c.state.user_inputs[withdrawn["preference_ref"]].value["source_account_ref"] == "A_LOCKED"
    )
    assert browser.ws.session.withdrawn_inputs == [withdrawn]
    assert withdrawn["pending_id"] not in c._pending
    # Only the device's input signatures ran: the preference and the amount.
    assert counts["confirm_input"] == 2
    assert counts["request_mfa"] == counts["submit_mfa"] == 0
    assert counts["approve"] == counts["dispatch"] == 0
    assert c.gateway.bank.execute_count == 0
    assert all(task["gesture"] is None for task in data["state"]["device"]["tasks"])


@pytest.mark.parametrize(
    "change", [{"owner": "C_OTHER"}, {"version": 2}], ids=["owner", "account-record"]
)
def test_e4_a_changed_source_account_closes_the_review(browser, monkeypatch, change):
    """Another owner, or any change to the source account's record after the review: the
    review is closed and its old token refused. (The currency cannot change: the schema
    fixes it to CNY; the explicit check below still requires it.)"""
    rkey = browser.send(PURCHASE)
    tkey = "T" + rkey
    saved = held_body(browser, tkey)
    c = browser.ws.controller
    account = c.state.accounts["A_CURRENT"]
    c.gateway.store.update(
        accounts={**c.state.accounts, "A_CURRENT": account.model_copy(update=change)}
    )
    counts = spied(monkeypatch, "confirm_input", "request_mfa", "submit_mfa", "approve", "dispatch")
    browser.post(f"/api/chat/responses/{rkey}/current", {})
    task = browser.task(tkey)
    assert task["step"] == "closed_facts" and task["gesture"] is None
    browser.fail(f"/api/device/tasks/{tkey}/confirm", saved, "gesture_unknown")
    assert counts == Counter() and not browser.ws.untrusted


@pytest.mark.parametrize(
    ("part", "key", "value"),
    [
        ("terms", "source_account_ref", "A_LOCKED"),
        ("account", "id", "A_LOCKED"),
        ("account", "owner", "C_OTHER"),
        ("account", "ccy", "USD"),
    ],
)
def test_e4_the_review_names_one_own_account_in_the_shown_currency(monkeypatch, part, key, value):
    """The explicit invariant (R3-E4): the signed preference's account is the reviewed
    purchase's source, that account is the customer's own and in the currency shown."""
    harness = logged_in()
    task = harness.ws.tasks["T" + harness.send(PURCHASE)]
    with harness.bound():
        assert purchase.valid(harness.ws, task) is True
        review = deepcopy(task.review)
        review[part][key] = value
        monkeypatch.setattr(task, "review", review)
        monkeypatch.setattr(purchase, "snapshot", lambda ws, rid: deepcopy(review))
        monkeypatch.setattr(purchase, "current_terms", lambda ws, t: deepcopy(review["terms"]))
        assert purchase.valid(harness.ws, task) is False


def test_e4_unreadable_terms_at_confirm_sign_nothing_in_the_product_layer(monkeypatch):
    """The product layer alone (the Streamlit and IM path): no terms to compare at confirm
    time, nothing is signed."""
    harness = logged_in()
    rid = harness.ws.rkeys[harness.send(PURCHASE)]
    monkeypatch.setattr(DomesticSession, "purchase_review_terms", lambda self, *a, **k: None)
    counts = spied(monkeypatch, "confirm_input", "request_mfa", "approve", "dispatch")
    with harness.bound():
        response = harness.session.confirm(rid)
    assert response["status"] == "clarify" and response["payload"]["terms_changed"] is True
    assert "withdrawn_input" not in response["payload"]
    assert counts == Counter()


# --- E-10: the same reference shown under another name or ordinal --------------------------


def renamed_product(controller, product_id):
    """The catalog entry under the same id gets other attributes, hence another name."""
    catalog = controller.state.catalog
    products = [
        item.model_copy(update={"min_hold_days": item.min_hold_days + 1})
        if item.product_id == product_id
        else item
        for item in catalog.products
    ]
    controller.gateway.store.update(catalog=catalog.model_copy(update={"products": products}))


def renamed_contact(controller, contact_ref):
    contact = controller.state.contacts[contact_ref]
    controller.gateway.store.update(
        contacts={
            **controller.state.contacts,
            contact_ref: contact.model_copy(update={"aliases": ["另一位联系人"]}),
        }
    )


def schedule_listing(page):
    page.red("T" + page.send(MONTHLY))
    listed = page.send(CANCEL_MONTHLY)
    return listed, page.response(listed)["schedule_cancel_select"]


@pytest.mark.parametrize("change", ["contact_name", "ordinal"])
def test_e10_a_schedule_choice_binds_its_shown_name_and_ordinal(browser, monkeypatch, change):
    listed, shown = schedule_listing(browser)
    c = browser.ws.controller
    (ref,), _ = service.schedule_cancel_choices(browser.ws, browser.ws.rkeys[listed])
    spa = c.state.authorizations[ref]
    before = compat.schedule_object(ref, controller=c)
    if change == "contact_name":
        renamed_contact(c, spa.target.contact_ref)
    else:
        # Another authorization of the same customer, ordered first: no new choice, but
        # the listed one is now the second.
        twin = spa.model_copy(update={"id": "!" + ref})
        c.gateway.store.update(authorizations={**c.state.authorizations, twin.id: twin})
    assert compat.schedule_object(ref, controller=c) != before
    assert service.schedule_cancel_choices(browser.ws, browser.ws.rkeys[listed])[0] == [ref]
    prepared = []
    monkeypatch.setattr(
        DomesticSession, "prepare_schedule_cancel", lambda self, *a: prepared.append(a)
    )
    body = {"choice": "c0", "list_version": shown["list_version"]}
    browser.fail(f"/api/chat/responses/{listed}/schedule-cancel", body, "card_changed")
    assert prepared == []


def test_e10_a_renamed_product_voids_the_wealth_choice(browser, monkeypatch):
    browser.post("/api/wealth/compare", {"objective": "net_return"})
    version = browser.listed("wealth")
    c = browser.ws.controller
    first = browser.ws.business["wealth_comparison"]["candidate_ids"][0]
    before = compat.display_names.label(first, c.state)
    renamed_product(c, first)
    assert compat.display_names.label(first, c.state) != before
    assert tasks.wealth_listing(browser.ws)[1] == first
    called = []
    monkeypatch.setattr(
        DemoController, "preview_investment", lambda self, *a, **k: called.append(a)
    )
    browser.fail("/api/wealth/purchase", {"choice": "c0", "list_version": version}, "card_changed")
    assert called == []


@pytest.mark.parametrize("change", ["product_name", "ordinal"])
def test_e10_the_redemption_list_binds_its_shown_name_and_ordinal(browser, monkeypatch, change):
    version = browser.listed("positions")
    c = browser.ws.controller
    (ref,) = tasks.positions(browser.ws)
    position = c.state.positions[ref]

    def label():
        return compat.object_facts({"objects": {"position_ref": ref}}, controller=c)[
            "position_object"
        ]

    before = label()
    if change == "product_name":
        renamed_product(c, position.product_ref)
    else:
        # A closed holding of the same customer, ordered first: not listed, but the listed
        # one is now the second.
        closed = position.model_copy(update={"id": "!" + ref, "status": "closed"})
        c.gateway.store.update(positions={closed.id: closed, **c.state.positions})
    assert label() != before
    assert tasks.positions(browser.ws) == [ref]
    called = []
    monkeypatch.setattr(DemoController, "redeem", lambda self, *a, **k: called.append(a))
    browser.fail("/api/wealth/redeem", {"choice": "c0", "list_version": version}, "card_changed")
    assert called == []


def test_e10_the_subscription_list_binds_the_shown_ordinal(browser, monkeypatch):
    browser.post("/api/subscriptions/query", {})
    assert finish(browser, newest(browser, "subscriptions")) == "done"
    version = browser.listed("subscriptions")
    c = browser.ws.controller
    ref = tasks.subscription_listing(browser.ws)["cancellable"][0]
    before = compat.subscription_object(ref, controller=c)
    twin = c.state.subscriptions[ref].model_copy(update={"id": "!" + ref, "status": "cancelled"})
    c.gateway.store.update(subscriptions={**c.state.subscriptions, twin.id: twin})
    assert compat.subscription_object(ref, controller=c) != before
    assert tasks.subscription_listing(browser.ws)["cancellable"][0] == ref
    called = []
    monkeypatch.setattr(
        DemoController, "cancel_subscription", lambda self, *a, **k: called.append(a)
    )
    browser.fail(
        "/api/subscriptions/cancel", {"choice": "c0", "list_version": version}, "card_changed"
    )
    assert called == []


@pytest.mark.parametrize("kind", ["redemption", "schedule_cancel"])
def test_e10_a_card_objects_label_source_is_bound_into_its_token(browser, monkeypatch, kind):
    c = browser.ws.controller
    if kind == "redemption":
        browser.pick("/api/wealth/redeem", "c0")
        tkey = newest(browser, "wealth")
    else:
        listed, shown = schedule_listing(browser)
        body = {"choice": "c0", "list_version": shown["list_version"]}
        tkey = "T" + browser.post(f"/api/chat/responses/{listed}/schedule-cancel", body)["rkey"]
    saved = held_body(browser, tkey)
    task = browser.ws.tasks[tkey]
    (card,) = compat.authoritative_cards(c, task.identity)
    with bound(browser.ws):
        info = tasks.task_step(browser.ws, task)
        before = tasks.digest(browser.ws, task, info)
    if kind == "redemption":
        renamed_product(c, c.state.positions[card["objects"]["position_ref"]].product_ref)
    else:
        spa = c.state.authorizations[card["objects"]["schedule_ref"]]
        renamed_contact(c, spa.target.contact_ref)
    with bound(browser.ws):
        after = tasks.task_step(browser.ws, task)
        assert after.step == info.step and info.op == "confirm"
        assert tasks.digest(browser.ws, task, after) != before
    counts = spied(monkeypatch, "approve", "dispatch")
    browser.fail(f"/api/device/tasks/{tkey}/confirm", saved, "card_changed")
    assert counts == Counter()


# --- E-14: every open route, a reachable success and a reachable refusal, counted apart ----

# The device's and the gateway's entry points, counted apart: an approval, an input
# signature, a code filled in by the device (never: INV-2.2), a code sent, a code
# submitted, a green read (GREEN, run without a card) and any other dispatch.
COUNTED = ("approve", "confirm_input", "complete_mfa", "request_mfa", "submit_mfa", "green")
COUNTED += ("dispatch",)
# What a route's success may count ("+": at least one); everything else must be zero.
EXPECTED: dict[str, dict[str, Any]] = {
    "/api/device/tasks/{tkey}/confirm": {"approve": 1, "dispatch": 1, "confirm_input": "+"},
    "/api/device/tasks/{tkey}/mfa/request": {"request_mfa": 1},
    "/api/device/tasks/{tkey}/mfa/verify": {"submit_mfa": 1},
    "/api/billing/analyze": {"green": "+"},
    "/api/wealth/compare": {"green": "+", "confirm_input": "+"},
    "/api/wealth/purchase": {"confirm_input": "+"},
    # On the preset's own isolated controller (INV-11.5), not the session's.
    "/api/presets/suitability-r4": {"green": "+", "confirm_input": "+"},
}
REFUSED_RESULTS = frozenset({"invalid", "stale", "locked", "locked_all", "unavailable"})


@dataclass
class Walk:
    """One route's walk: the setup requests are not counted, the two measured ones are."""

    app: Any
    route: str
    monkeypatch: Any
    tmp_path: Path
    counts: dict = field(default_factory=dict)
    now: Any = None
    results: dict = field(default_factory=dict)

    def __post_init__(self):
        owners = {name: Device for name in ("approve", "confirm_input", "complete_mfa")}
        owners.update({name: Gateway for name in ("dispatch", "request_mfa", "submit_mfa")})
        for name, owner in owners.items():
            original = getattr(owner, name)

            def wrapped(obj, *args, _name=name, _original=original, **kwargs):
                if self.now is not None:
                    key = _name
                    if _name == "dispatch":
                        key = "green" if str(args[0].kind) in GREEN else "dispatch"
                    self.counts[self.now][key] += 1
                return _original(obj, *args, **kwargs)

            self.monkeypatch.setattr(owner, name, wrapped)

    def page(self, *, login=True, view="judge"):
        page = Browser(self.app, view=view)
        if login:
            page.login()
        return page

    def measure(self, state, page, path, body=None, *, headers=None, client=None):
        """One request, its counts kept under ``state`` ("positive" or "negative")."""
        client = client or page.client
        self.counts[state] = Counter()
        self.now = state
        try:
            response = client.post(path, {} if body is None else body, headers=headers)
        finally:
            self.now = None
        data = client.json(response)
        if page is not None and isinstance(data, dict) and "state" in data:
            page.state = data["state"]
        self.results[state] = (response.code, data)
        return data

    def hosted(self, rule=None):
        """The test-only fake deployment; ``rule`` is what its steps answer (FakeModel)."""
        for name, value in environ(self.tmp_path).items():
            self.monkeypatch.setenv(name, value)
        FakeModel(self.monkeypatch, rule=rule)


def nonce(page):
    return page.post("/api/live/challenge", {})["nonce"]


def unlocked(page):
    value = nonce(page)
    body = {"nonce": value, "proof": access_proof(ACCESS_CODE, value)}
    assert page.post("/api/live/unlock", body)["result"] == "accepted"


def walk_session(w):
    client = WebClient(w.app)
    w.measure("negative", None, "/api/session", {}, headers={"X-Corridor-Web": None}, client=client)
    w.measure("positive", None, "/api/session", {}, client=client)


def walk_reset(w):
    page = w.page()
    w.measure("negative", page, "/api/session/reset", {}, headers=STALE)
    w.measure("positive", page, "/api/session/reset", {})


def walk_login(w):
    page = w.page(login=False)
    w.measure("negative", page, "/api/session/login", {}, headers=STALE)
    w.measure("positive", page, "/api/session/login", {})


def walk_view(w):
    page = w.page()
    w.measure("negative", page, "/api/session/view", {"view": "nobody"})
    w.measure("positive", page, "/api/session/view", {"view": "customer"})


def walk_locale(w):
    page = w.page()
    w.measure("negative", page, "/api/session/locale", {"locale": "fr"})
    w.measure("positive", page, "/api/session/locale", {"locale": "en"})


def walk_message(w):
    page = w.page()
    body = {"text": YELLOW, "external_files": 0, "client_msg_id": "walk-message-1"}
    w.measure("positive", page, "/api/chat/message", body)
    w.measure("negative", page, "/api/chat/message", {**body, "text": RED})


def walk_shortcut(w):
    page = w.page()
    # The wealth shortcut is not opened by this build (P-DIFF-17).
    body = {"shortcut": "wealth", "client_msg_id": "walk-shortcut-1"}
    w.measure("negative", page, "/api/chat/shortcut", body)
    w.measure("positive", page, "/api/chat/shortcut", {**body, "shortcut": "transfer"})


def walk_current(w):
    page = w.page()
    older = page.send(YELLOW)
    page.send("查询余额")
    w.measure("negative", page, "/api/chat/responses/R99/current", {})
    w.measure("positive", page, f"/api/chat/responses/{older}/current", {})


def walk_recipient(w):
    page = w.page()
    rkey = page.send(CLARIFY_PLAIN)
    path = f"/api/chat/responses/{rkey}/recipient"
    w.measure("negative", page, path, {"choice": "c9"})
    w.measure("positive", page, path, {"choice": "c0"})


def walk_suggestion(w):
    # A suggestion is listed only under a live clarification: the fake deployment answers
    # the suggestion step with one transfer. A stale list version is refused before the
    # desk is reached; the click sends the sentence as a message, which prepares a card
    # and approves, signs, sends or dispatches nothing.
    w.hosted(rule=suggest_rule(TRANSFER))
    page = w.page()
    unlocked(page)
    assert page.post("/api/live/toggle", {"on": True})["live"]["toggle"]["on"] is True
    rkey = page.send(UNCLEAR)
    version = page.response(rkey)["suggestion_select"]["list_version"]
    path = f"/api/chat/responses/{rkey}/suggestion"
    body = {"choice": "c0", "list_version": version, "client_msg_id": "walk-suggestion-1"}
    w.measure("negative", page, path, {**body, "list_version": "0" * 32})
    w.measure("positive", page, path, body)


def walk_subscription_choice(w):
    page = w.page()
    rkey = page.send("取消订阅")
    version = page.response(rkey)["subscription_select"]["list_version"]
    path = f"/api/chat/responses/{rkey}/subscription"
    w.measure("negative", page, path, {"choice": "c0", "list_version": "0" * 32})
    w.measure("positive", page, path, {"choice": "c0", "list_version": version})


def walk_schedule_choice(w):
    page = w.page()
    listed, shown = schedule_listing(page)
    path = f"/api/chat/responses/{listed}/schedule-cancel"
    w.measure("negative", page, path, {"choice": "c0", "list_version": "0" * 32})
    w.measure("positive", page, path, {"choice": "c0", "list_version": shown["list_version"]})


def stale_gate(version):
    return ("A" if version[0] != "A" else "B") + version[1:]


def walk_gate_answer(w):
    page = w.page()
    rkey = page.send(NEW)
    version = page.gate(rkey)["version"]
    path = f"/api/chat/responses/{rkey}/gate/answer"
    w.measure("negative", page, path, {"gate_version": stale_gate(version), "answers": SAFE})
    w.measure("positive", page, path, {"gate_version": version, "answers": SAFE})


def walk_gate_retry(w):
    page = w.page()
    rkey = page.send(NEW)
    page.answer(rkey, {"verified": "yes", "guided": "yes"})
    version = page.gate(rkey)["version"]
    path = f"/api/chat/responses/{rkey}/gate/retry"
    w.measure("negative", page, path, {"gate_version": stale_gate(version)})
    w.measure("positive", page, path, {"gate_version": version})


def walk_birthday(w):
    page = w.page()
    rkey = page.send("帮我准备生日")
    choice = next(
        item["key"] for item in page.response(rkey)["birthday"]["choices"] if item["enabled"]
    )
    path = f"/api/chat/responses/{rkey}/birthday"
    w.measure("negative", page, path, {"choice": "c9"})
    w.measure("positive", page, path, {"choice": choice})


def walk_chat_stop(w, path="/api/chat/stop"):
    page = w.page(login=False)
    w.measure("negative", page, path, {})
    page.login()
    w.measure("positive", page, path, {})


def walk_handoff(w):
    walk_chat_stop(w, "/api/chat/handoff")


def walk_compensate(w):
    page = w.page()
    dispatched_plan(page)
    version = page.tab("operator")["compensation"]["compensate_version"]
    w.measure("negative", page, "/api/chat/compensate", {"plan_version": "0" * 32})
    w.measure("positive", page, "/api/chat/compensate", {"plan_version": version})


def walk_nodes(w):
    page = w.page()
    plan = "T" + page.send(BIRTHDAY)
    path = f"/api/device/tasks/{plan}/nodes"
    w.measure("negative", page, path, {"card_version": "0" * 32, "nodes": ["n0", "n1"]})
    version = page.task(plan)["nodes_version"]
    w.measure("positive", page, path, {"card_version": version, "nodes": ["n0", "n1"]})


def walk_gesture(w, op, text, setup=()):
    page = w.page()
    tkey = "T" + page.send(text)
    for step in setup:
        page.device(tkey, step)
    path = {"confirm": "confirm", "mfa_request": "mfa/request", "mfa_verify": "mfa/verify"}[op]
    extra = {"code": page.sms_code()} if op == "mfa_verify" else {}
    gesture = page.gesture(tkey)
    wrong = ("A" if gesture["token"][0] != "A" else "B") + gesture["token"][1:]
    body = {"gesture": wrong, "card_version": gesture["card_version"], **extra}
    w.measure("negative", page, f"/api/device/tasks/{tkey}/{path}", body)
    body = {**body, "gesture": gesture["token"]}
    w.measure("positive", page, f"/api/device/tasks/{tkey}/{path}", body)


def walk_confirm(w):
    walk_gesture(w, "confirm", YELLOW)


def walk_mfa_request(w):
    walk_gesture(w, "mfa_request", RED, ("confirm",))


def walk_mfa_verify(w):
    walk_gesture(w, "mfa_verify", RED, ("confirm", "mfa_request"))


def walk_cancel(w):
    page = w.page()
    rkey = page.send(YELLOW)
    w.measure("negative", page, "/api/device/tasks/TR99/cancel", {})
    w.measure("positive", page, f"/api/device/tasks/T{rkey}/cancel", {})


def walk_stop(w):
    page = w.page()
    page.send(YELLOW)
    w.measure("negative", page, "/api/stop", {}, headers=STALE)
    w.measure("positive", page, "/api/stop", {})


def walk_logged_out_first(path, body=None):
    def walk(w):
        page = w.page(login=False)
        w.measure("negative", page, path, body or {})
        page.login()
        w.measure("positive", page, path, body or {})

    return walk


def walk_subscriptions_cancel(w):
    page = w.page()
    page.post("/api/subscriptions/query", {})
    assert finish(page, newest(page, "subscriptions")) == "done"
    version = page.listed("subscriptions")
    path = "/api/subscriptions/cancel"
    w.measure("negative", page, path, {"choice": "c0", "list_version": "0" * 32})
    w.measure("positive", page, path, {"choice": "c0", "list_version": version})


def walk_purchase(w):
    page = w.page()
    page.post("/api/wealth/compare", {"objective": "net_return"})
    version = page.listed("wealth")
    path = "/api/wealth/purchase"
    w.measure("negative", page, path, {"choice": None, "list_version": "0" * 32})
    w.measure("positive", page, path, {"choice": None, "list_version": version})


def walk_redeem(w):
    page = w.page()
    version = page.listed("positions")
    path = "/api/wealth/redeem"
    w.measure("negative", page, path, {"choice": "c0", "list_version": "0" * 32})
    w.measure("positive", page, path, {"choice": "c0", "list_version": version})


def walk_preset(w):
    page = w.page()
    w.measure("negative", page, "/api/presets/suitability-r4", {}, headers=STALE)
    w.measure("positive", page, "/api/presets/suitability-r4", {})


def walk_live_challenge(w):
    # No hosted deployment is configured yet: the switch is unavailable.
    w.measure("negative", w.page(), "/api/live/challenge", {})
    w.hosted()
    w.measure("positive", w.page(), "/api/live/challenge", {})


def walk_live_unlock(w):
    w.hosted()
    page = w.page()
    value = nonce(page)
    body = {"nonce": value, "proof": access_proof("wrong-code-0", value)}
    w.measure("negative", page, "/api/live/unlock", body)
    value = nonce(page)
    body = {"nonce": value, "proof": access_proof(ACCESS_CODE, value)}
    w.measure("positive", page, "/api/live/unlock", body)


def walk_live_select(w):
    w.hosted()
    page = w.page()
    unlocked(page)
    w.measure("negative", page, "/api/live/select", {"deployment": "no-such-deployment"})
    w.measure("positive", page, "/api/live/select", {"deployment": DEPLOYMENTS[1].id})


def walk_live_toggle(w):
    w.hosted()
    page = w.page()
    w.measure("negative", page, "/api/live/toggle", {"on": True})
    unlocked(page)
    w.measure("positive", page, "/api/live/toggle", {"on": True})


WALKS = {
    "/api/session": walk_session,
    "/api/session/reset": walk_reset,
    "/api/session/login": walk_login,
    "/api/session/view": walk_view,
    "/api/session/locale": walk_locale,
    "/api/chat/message": walk_message,
    "/api/chat/shortcut": walk_shortcut,
    "/api/chat/responses/{rkey}/current": walk_current,
    "/api/chat/responses/{rkey}/recipient": walk_recipient,
    "/api/chat/responses/{rkey}/suggestion": walk_suggestion,
    "/api/chat/responses/{rkey}/subscription": walk_subscription_choice,
    "/api/chat/responses/{rkey}/schedule-cancel": walk_schedule_choice,
    "/api/chat/responses/{rkey}/gate/answer": walk_gate_answer,
    "/api/chat/responses/{rkey}/gate/retry": walk_gate_retry,
    "/api/chat/responses/{rkey}/birthday": walk_birthday,
    "/api/chat/stop": walk_chat_stop,
    "/api/chat/handoff": walk_handoff,
    "/api/chat/compensate": walk_compensate,
    "/api/device/tasks/{tkey}/nodes": walk_nodes,
    "/api/device/tasks/{tkey}/confirm": walk_confirm,
    "/api/device/tasks/{tkey}/mfa/request": walk_mfa_request,
    "/api/device/tasks/{tkey}/mfa/verify": walk_mfa_verify,
    "/api/device/tasks/{tkey}/cancel": walk_cancel,
    "/api/stop": walk_stop,
    "/api/subscriptions/query": walk_logged_out_first("/api/subscriptions/query"),
    "/api/subscriptions/cancel": walk_subscriptions_cancel,
    "/api/billing/analyze": walk_logged_out_first("/api/billing/analyze"),
    "/api/wealth/compare": walk_logged_out_first(
        "/api/wealth/compare", {"objective": "net_return"}
    ),
    "/api/wealth/purchase": walk_purchase,
    "/api/wealth/redeem": walk_redeem,
    "/api/presets/suitability-r4": walk_preset,
    "/api/live/challenge": walk_live_challenge,
    "/api/live/unlock": walk_live_unlock,
    "/api/live/select": walk_live_select,
    "/api/live/toggle": walk_live_toggle,
}


def open_posts():
    return {
        template
        for template, action in handlers.POSTS.items()
        if set(action.needs) <= web_package.FEATURES
    }


def test_e14_every_open_post_route_has_a_walk_and_the_closed_ones_are_named():
    assert set(WALKS) == open_posts() | {"/api/session", "/api/session/reset"}
    assert set(handlers.POSTS) - open_posts() == {AA, *LAB_POSTS}


def refused(code, data):
    if 400 <= code < 500:
        return data.get("ok") is False
    # The live unlock answers a wrong code as its own refusal result, not as an error.
    return code == 200 and data.get("result") in REFUSED_RESULTS


@pytest.mark.parametrize("route", sorted(WALKS))
def test_e14_each_open_route_succeeds_and_refuses_with_its_counts_kept_apart(
    web_app, monkeypatch, tmp_path, route
):
    """R2/R3-E14: for every open route, one reachable success and one reachable refusal,
    and per state the approvals, input signatures, codes sent and submitted, green reads
    and dispatches counted apart. A refusal counts nothing; a success counts only what
    its route is for (``EXPECTED``)."""
    walk = Walk(web_app, route, monkeypatch, tmp_path)
    WALKS[route](walk)
    code, data = walk.results["positive"]
    assert code == 200 and data.get("ok") is True, (route, code, data)
    if route == "/api/live/unlock":
        assert data["result"] == "accepted"
    code, data = walk.results["negative"]
    assert refused(code, data), (route, code, data)
    negative, positive = walk.counts["negative"], walk.counts["positive"]
    assert sum(negative.values()) == 0, (route, negative)
    expected = EXPECTED.get(route, {})
    for name in COUNTED:
        want = expected.get(name, 0)
        if want == "+":
            assert positive[name] > 0, (route, name, positive)
        else:
            assert positive[name] == want, (route, name, positive)


def test_e14_aa_is_unreachable_in_the_submission_build(web_app, monkeypatch):
    """AA (P1) is not opened: its route answers 404 in both views with a well-formed body,
    its service function never runs, and no reply carries an AA control."""
    assert "aa" not in web_package.FEATURES
    assert handlers.POSTS[AA].needs == ("aa",)
    called = []
    monkeypatch.setattr(service, "complete_aa", lambda *a, **k: called.append(a))
    counts = spied(monkeypatch, "approve", "confirm_input", "request_mfa", "submit_mfa", "dispatch")
    for view in ("customer", "judge"):
        page = Browser(web_app, view=view)
        page.login()
        rkey = page.send("帮我准备生日")
        data = page.post(f"/api/chat/responses/{rkey}/aa", {"choices": ["c0"]}, expect=404)
        assert data["error"]["code"] == "not_found"
        assert page.state["capabilities"]["aa"] is False
        assert all("aa" not in shown for shown in page.state["chat"]["responses"].values())
    assert called == [] and counts == Counter()


# --- E-15: a business preview stopped, retried or repeated ---------------------------------


def test_e15_a_repeated_preview_keeps_one_task_one_token_and_signs_nothing_again(
    browser, monkeypatch
):
    """The page's request id returns the same card for a preview sent again (a response
    lost and retried, a double submit): one task and one token per pending (the fix of
    R3-E15: the repeated card had been registered as a second task with its own token),
    and no input is signed a second time."""
    counts = spied(monkeypatch, "confirm_input", "approve")
    browser.post("/api/subscriptions/query", {})
    browser.post("/api/subscriptions/query", {})
    assert len(business(browser, "subscriptions")) == 1
    assert len(executable(browser, "subscriptions")) == 1
    browser.post("/api/wealth/compare", {"objective": "net_return"})
    signed = counts["confirm_input"]
    browser.pick("/api/wealth/purchase", None)
    first = counts["confirm_input"] - signed
    assert first > 0
    browser.pick("/api/wealth/purchase", None)
    browser.pick("/api/wealth/redeem", "c0")
    browser.pick("/api/wealth/redeem", "c0")
    assert counts["confirm_input"] - signed == first
    wealth = business(browser, "wealth")
    assert len(wealth) == 2 and all(task["gesture"] is not None for task in wealth)
    held = [(task.source, task.identity) for task in browser.ws.tasks.values()]
    assert len(held) == len(set(held))
    # The comparison is a green read (GREEN); nothing was approved.
    assert counts["approve"] == 0


def test_e15_two_identical_previews_at_once_keep_one_task(browser):
    """Requests of one session run one at a time under its lock, so two identical POSTs
    sent together are the repeated case above, in either order."""
    with ThreadPoolExecutor(2) as pool:
        answers = list(
            pool.map(lambda _: browser.client.post("/api/subscriptions/query", {}), range(2))
        )
    assert [answer.code for answer in answers] == [200, 200]
    browser.state = browser.get("/api/state")["state"]
    assert len(business(browser, "subscriptions")) == 1
    assert len(executable(browser, "subscriptions")) == 1


@pytest.mark.parametrize("stop", ["chat", "global"])
def test_e15_a_stop_between_a_preview_and_its_retry_leaves_one_executable_card(
    browser, monkeypatch, stop
):
    browser.post("/api/subscriptions/query", {})
    (old,) = business(browser, "subscriptions")
    saved = held_body(browser, old["key"])
    identity = browser.ws.tasks[old["key"]].identity
    if stop == "chat":
        browser.send("停止")
    else:
        browser.post("/api/stop", {})
    browser.post("/api/subscriptions/query", {})
    (shown,) = executable(browser, "subscriptions")
    assert shown["key"] != old["key"]
    assert browser.ws.tasks[shown["key"]].identity != identity
    counts = spied(monkeypatch, "approve", "dispatch")
    status = browser.client.post(f"/api/device/tasks/{old['key']}/confirm", saved).code
    assert status in (404, 409)
    assert counts == Counter()
    assert finish(browser, shown["key"]) == "done"
    assert counts["dispatch"] == 1


def test_e15_a_settled_card_is_not_offered_again_by_a_repeated_preview(browser, monkeypatch):
    browser.post("/api/subscriptions/query", {})
    (task,) = business(browser, "subscriptions")
    saved = held_body(browser, task["key"])
    assert finish(browser, task["key"]) == "done"
    counts = spied(monkeypatch, "confirm_input", "approve", "dispatch")
    browser.post("/api/subscriptions/query", {})
    assert executable(browser, "subscriptions") == []
    assert [item["key"] for item in business(browser, "subscriptions")] == [task["key"]]
    assert browser.client.post(f"/api/device/tasks/{task['key']}/confirm", saved).code == 409
    assert counts == Counter()


def test_e15_a_vanished_pending_is_asked_again_once(browser):
    """A cached preview whose pending is gone without a result (as a Stop said in the
    chat leaves it) asks again with a new request id, once (P-DIFF-25)."""
    browser.post("/api/subscriptions/query", {})
    (old,) = business(browser, "subscriptions")
    identity = browser.ws.tasks[old["key"]].identity
    del browser.ws.controller._pending[identity]
    browser.post("/api/subscriptions/query", {})
    (shown,) = executable(browser, "subscriptions")
    renewed = browser.ws.tasks[shown["key"]].identity
    assert renewed != identity
    browser.post("/api/subscriptions/query", {})
    (again,) = executable(browser, "subscriptions")
    assert browser.ws.tasks[again["key"]].identity == renewed


def test_e15_one_compensation_card_at_a_time(browser, monkeypatch):
    dispatched_plan(browser)
    browser.compensate()
    (first,) = business(browser, "compensation")
    calls = []
    original = DemoController.compensate

    def counted(self, plan_id):
        calls.append(plan_id)
        return original(self, plan_id)

    monkeypatch.setattr(DemoController, "compensate", counted)
    data = browser.compensate(expect=409)
    assert data["error"]["code"] == "not_applicable" and calls == []
    assert [task["key"] for task in business(browser, "compensation")] == [first["key"]]
    assert not browser.ws.untrusted
    # Once the open card is finished, Compensate is open again.
    assert finish(browser, first["key"]) == "done"
    browser.compensate()
    assert len(calls) == 1
