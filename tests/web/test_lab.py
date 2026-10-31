"""Authorization lab: real local controllers, explicit gestures and isolated lifetimes.

The lab opens in the judge view of a logged-in session only (R2); its SMS is read from
the child's own phone page (``/api/phone/<child pair>``), never from a lab route. The
submission build closes the lab (RW-4''): these tests open its capability explicitly.
"""

from copy import deepcopy

import pytest

import corridor.web as web_package
from corridor.gateway.service import Gateway
from corridor.sim.device import Device
from corridor.web import compat, errors, lab, service
from tests.web.test_api_chat import CODE, Browser


@pytest.fixture
def web_settings():
    return {"features": web_package.FEATURES | {lab.FEATURE}}


def start(page, scenario="yellow"):
    if not page.state["session"]["logged_in"]:
        page.login()
    return page.post("/api/lab/start", {"scenario": scenario, "mode": "scripted"})["lab"]


def child_phone(page):
    pair = page.ws.demos[lab.SLOT]["session"].pair
    return page.client.json(page.client.get(f"/api/phone/{pair}?lang=zh-CN&view=judge"))


def current(vm):
    return next(task for task in vm["state"]["device"]["tasks"] if task["current"])


def mutate(page, path, vm, **values):
    return page.post(path, {"lab_generation": vm["session_generation"], **values})["lab"]


def gesture(page, vm, **extra):
    task = current(vm)
    shown = task["gesture"]
    return mutate(
        page,
        "/api/lab/gesture",
        vm,
        tkey=task["key"],
        op=shown["op"],
        gesture=shown["token"],
        card_version=shown["card_version"],
        **extra,
    )


@pytest.fixture
def calls(monkeypatch):
    found = []
    for owner, names in (
        (Device, ("approve", "complete_mfa")),
        (Gateway, ("dispatch", "request_mfa", "submit_mfa")),
    ):
        for name in names:
            original = getattr(owner, name)

            def wrap(obj, *args, _name=name, _original=original, **kwargs):
                found.append(_name)
                return _original(obj, *args, **kwargs)

            monkeypatch.setattr(owner, name, wrap)
    return found


def test_get_does_not_create_or_advance_an_experiment(web_app, calls):
    page = Browser(web_app)
    page.login()
    before = deepcopy(page.ws.controller.snapshot())
    assert page.get("/api/lab")["lab"] == {"active": False, "mode": "scripted"}
    assert page.ws.demos == {}
    assert page.ws.controller.snapshot() == before
    vm = start(page)
    child = page.ws.demos[lab.SLOT]["session"]
    before = deepcopy((child.controller.snapshot(), child.gestures, child.transcript))
    for _ in range(3):
        assert page.get("/api/lab")["lab"] == vm
    assert (child.controller.snapshot(), child.gestures, child.transcript) == before
    assert calls == []


@pytest.mark.parametrize("scenario", ["yellow", "red"])
def test_start_only_prepares_and_requires_explicit_gestures(web_app, calls, scenario):
    page = Browser(web_app)
    page.login()
    parent = deepcopy(page.ws.controller.snapshot())
    vm = start(page, scenario)
    assert vm["mode"] == "scripted" and vm["source"] == "synthetic_controller_gateway"
    assert current(vm)["gesture"]["op"] == "confirm"
    assert vm["proof"]["dispatches"] == 0 and vm["proof"]["bank_receipts"] == []
    assert calls == []
    vm = gesture(page, vm)
    if scenario == "red":
        assert vm["proof"]["dispatches"] == 0
        assert current(vm)["gesture"]["op"] == "mfa_request"
        vm = gesture(page, vm)
        assert "request_mfa" in calls and "submit_mfa" not in calls
        assert vm["proof"]["dispatches"] == 0
        phone = child_phone(page)
        code = CODE.search(next(m["text"] for m in phone["messages"] if m["latest"])).group()
        assert code not in str(vm)
        vm = gesture(page, vm, code=code)
        assert vm["proof"]["dispatches"] == 0
        assert current(vm)["gesture"]["op"] == "confirm"
        vm = gesture(page, vm)
    assert current(vm)["step"] == "done"
    assert vm["proof"]["dispatches"] == 1
    assert len(vm["proof"]["bank_receipts"]) == 1
    assert vm["proof"]["bank_receipts"][0]["status"] == "settled"
    assert vm["proof"]["audit_valid"] is True
    assert calls.count("approve") == 1 and calls.count("dispatch") == 1
    assert page.ws.controller.snapshot() == parent


def test_changing_recipient_revokes_old_gesture_without_approving(web_app, calls):
    page = Browser(web_app)
    before = start(page)
    vm = mutate(page, "/api/lab/change", before)
    old_card, new_card = vm["previous"]["card"], vm["card"]
    old_facts = {row["key"]: row["value"] for row in old_card["facts"]}
    new_facts = {row["key"]: row["value"] for row in new_card["facts"]}
    assert old_facts["objects.contact_ref"] != new_facts["objects.contact_ref"]
    assert old_card["amount_text"] == new_card["amount_text"]
    assert old_facts["objects.source_ref"] == new_facts["objects.source_ref"]
    assert vm["previous"]["task"]["gesture"] is None
    assert vm["proof"]["card_changed"] is True
    assert calls == []
    vm = mutate(page, "/api/lab/try-previous", vm)
    assert vm["last_attempt"]["status"] == "refused"
    # Global withdrawal removes the old task, so the verify-only check refuses it at
    # lookup; the old token on the new card is refused as another task's token.
    assert vm["last_attempt"]["operation_status"] == 404
    assert vm["last_attempt"]["code"] == "not_found"
    assert [check["code"] for check in vm["last_attempt"]["checks"]] == [
        "not_found",
        "gesture_mismatch",
    ]
    assert vm["proof"]["dispatches"] == 0 and calls == []
    assert vm["previous_attempted"] is True
    vm = gesture(page, vm)
    assert vm["proof"]["dispatches"] == 1
    assert calls.count("approve") == 1


def test_changed_card_can_still_require_independent_mfa(web_app, calls):
    page = Browser(web_app)
    vm = mutate(page, "/api/lab/change", start(page, "red"))
    vm = mutate(page, "/api/lab/try-previous", vm)
    assert calls == [] and vm["last_attempt"]["status"] == "refused"
    vm = gesture(page, vm)
    assert current(vm)["gesture"]["op"] == "mfa_request"
    vm = gesture(page, vm)
    assert current(vm)["gesture"]["op"] == "mfa_verify"
    assert vm["proof"]["dispatches"] == 0
    assert "approve" not in calls and "submit_mfa" not in calls


def test_missing_mfa_input_never_fills_a_code_or_dispatches(web_app, calls):
    page = Browser(web_app)
    vm = gesture(page, gesture(page, start(page, "red")))
    task, shown = current(vm), current(vm)["gesture"]
    page.post(
        "/api/lab/gesture",
        {
            "lab_generation": vm["session_generation"],
            "tkey": task["key"],
            "op": "mfa_verify",
            "gesture": shown["token"],
            "card_version": shown["card_version"],
        },
        expect=422,
    )
    assert "submit_mfa" not in calls and "complete_mfa" not in calls and "dispatch" not in calls
    assert page.get("/api/lab")["lab"] == vm


def test_phone_is_scoped_to_the_current_parents_child(web_app):
    owner, other = Browser(web_app), Browser(web_app)
    other.login()
    vm = gesture(owner, gesture(owner, start(owner, "red")))
    assert vm["proof"]["dispatches"] == 0
    child_pair = owner.ws.demos[lab.SLOT]["session"].pair
    assert child_pair in vm["state"]["device"]["sms"]["url"]
    own_phone = child_phone(owner)
    assert any(message["status"] == "active" for message in own_phone["messages"])
    # No lab route reads an inbox; the other parent's state never names this pairing.
    assert owner.client.get("/api/lab/phone").code == 404
    assert other.get("/api/lab")["lab"] == {"active": False, "mode": "scripted"}
    assert child_pair not in str(other.get("/api/state"))
    owner.post("/api/stop", {})
    gone = owner.get(f"/api/phone/{child_pair}")
    assert gone["messages"] == [] and gone["unpaired"]


def test_confirm_replay_never_dispatches_twice(web_app, calls):
    page = Browser(web_app)
    vm = start(page)
    task, shown = current(vm), current(vm)["gesture"]
    body = {
        "lab_generation": vm["session_generation"],
        "tkey": task["key"],
        "op": shown["op"],
        "gesture": shown["token"],
        "card_version": shown["card_version"],
    }
    first = page.post("/api/lab/gesture", body)["lab"]
    second = page.post("/api/lab/gesture", body)["lab"]
    assert first["proof"]["dispatches"] == second["proof"]["dispatches"] == 1
    assert first["proof"]["bank_receipts"] == second["proof"]["bank_receipts"]
    assert second["last_attempt"]["status"] == "refused"
    assert calls.count("dispatch") == calls.count("approve") == 1


def test_mode_and_scenarios_are_closed_inputs(web_app, calls):
    page = Browser(web_app)
    for body in (
        {"scenario": "yellow"},
        {"scenario": "yellow", "mode": "live"},
        {"scenario": "custom", "mode": "scripted"},
    ):
        page.post("/api/lab/start", body, expect=400)
    assert page.ws.demos == {} and calls == []


@pytest.mark.parametrize("failure", [errors.WebOutcomeUnknown, RuntimeError])
def test_uncertain_result_closes_child_and_clears_cached_gestures(web_app, monkeypatch, failure):
    page = Browser(web_app)
    vm = start(page)
    task, shown = current(vm), current(vm)["gesture"]

    def unknown(*args, **kwargs):
        raise failure()

    monkeypatch.setattr(service, "device_confirm", unknown)
    page.post(
        "/api/lab/gesture",
        {
            "lab_generation": vm["session_generation"],
            "tkey": task["key"],
            "op": shown["op"],
            "gesture": shown["token"],
            "card_version": shown["card_version"],
        },
        expect=500,
    )
    closed = page.get("/api/lab")["lab"]
    assert current(closed)["gesture"] is None and closed["can_change"] is False
    assert closed["last_attempt"]["status"] == "unknown"
    assert closed["proof"]["dispatches"] == 0
    assert page.ws.demos[lab.SLOT]["session"].untrusted
    # The failure stays inside the child: the banking session is not distrusted (R2-E7).
    assert not page.ws.untrusted


def test_child_generation_and_gesture_do_not_cross_sessions_or_restarts(web_app):
    first, second = Browser(web_app), Browser(web_app)
    one, two = start(first), start(second)
    body = {"lab_generation": one["session_generation"]}
    assert second.post("/api/lab/change", body, expect=409)["error"]["code"] == "stale_generation"
    stolen = current(one)["gesture"]
    rejected = mutate(
        second,
        "/api/lab/gesture",
        two,
        tkey=current(two)["key"],
        op=stolen["op"],
        gesture=stolen["token"],
        card_version=stolen["card_version"],
    )
    assert rejected["last_attempt"]["status"] == "refused"
    assert rejected["proof"]["dispatches"] == 0
    old = first.ws.demos[lab.SLOT]["session"]
    replacement = start(first)
    assert replacement["session_generation"] != one["session_generation"]
    assert not old.gestures and old.untrusted
    assert first.post("/api/lab/change", body, expect=409)["error"]["code"] == "stale_generation"


@pytest.mark.parametrize("how", ["stop", "reset", "close", "evict"])
def test_parent_retirement_revokes_child_tokens_and_sms(web_app, how):
    page = Browser(web_app)
    vm = gesture(page, start(page, "red"))
    gesture(page, vm)
    parent = page.ws
    child = parent.demos[lab.SLOT]["session"]
    assert child.gestures and any(s.status == "active" for s in compat.OUTBOX.inbox(child.pair))
    if how == "stop":
        page.post("/api/stop", {})
        assert page.get("/api/lab")["lab"]["active"] is False
    elif how == "reset":
        page.post("/api/session/reset", {})
        assert page.get("/api/lab")["lab"]["active"] is False
    elif how == "close":
        web_app.settings["store"].close(parent)
    else:
        web_app.settings["store"]._max = 1
        Browser(web_app)
    assert lab.SLOT not in parent.demos
    assert child.untrusted and child.gestures == {}
    assert all(s.status != "active" for s in compat.OUTBOX.inbox(child.pair))
    assert not web_app.settings["store"].pairs.allowed(child.pair)


def test_transport_rejections_leave_lab_unchanged(web_app):
    page = Browser(web_app)
    vm = start(page)
    body = {"lab_generation": vm["session_generation"]}
    for headers, status in (
        ({"Origin": "http://other.invalid"}, 403),
        ({"X-Corridor-Generation": "x" * 12}, 409),
    ):
        page.post("/api/lab/change", body, headers=headers, expect=status)
        assert page.get("/api/lab")["lab"] == vm
    page.post("/api/lab/change", {**body, "card": {}}, expect=400)
    assert page.get("/api/lab")["lab"] == vm


def test_cached_view_switch_does_not_expose_judge_evidence(web_app):
    page = Browser(web_app)
    start(page)
    page.post("/api/session/view", {"view": "customer"})
    # The customer view receives no lab body at all (R2-S1).
    refused = page.get("/api/lab", expect=409)
    assert refused["error"]["code"] == "not_applicable" and "lab" not in refused
    page.post("/api/session/locale", {"locale": "en"})
    page.post("/api/session/view", {"view": "judge"})
    vm = page.get("/api/lab")["lab"]
    assert vm["state"]["locale"] == "en" and vm["state"]["view"] == "judge"
