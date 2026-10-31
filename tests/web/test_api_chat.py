"""B1: the chat endpoints over HTTP, with their error codes and attached state.

The same rules as the service tests, through the real handlers, session store and view
models: replies, the current-reply rule, the recipient check, the phrase gate, Cancel and
handoff, and the words fixed when sent.
"""

import re

import pytest

from tests.unit.test_ui_scam_friction import CLARIFY, CLARIFY_PLAIN, FRAUD, NEW, SAFE, Spy
from tests.web.conftest import WebClient, ws_of
from tests.web.test_tasks import (
    DIRECTED_NEW,
    HELD,
    RED,
    YELLOW,
    inv22,  # noqa: F401  (INV-2.2 probe)
)

PAIR = re.compile(r"/phone/([A-Za-z0-9_-]{16,64})")
CODE = re.compile(r"(?<![0-9])[0-9]{6}(?![0-9])")


class Browser:
    """One browser tab on the demo: the page's own requests and the state it adopted."""

    def __init__(self, app, *, view="judge", locale=None):
        self.app = app
        self.client = WebClient(app)
        self.bodies = []
        opened = self.client.open(view=view, locale=locale)
        self.state = opened["state"]
        self._ids = 0

    @property
    def ws(self):
        return ws_of(self.app, self.client)

    def post(self, path, body=None, *, expect=200, headers=None, raw=None):
        response = self.client.post(path, body, headers=headers, raw=raw)
        data = self.client.json(response)
        self.bodies.append((path, response.body.decode("utf-8")))
        assert response.code == expect, (path, response.code, data)
        if "state" in data:
            assert data["state"]["generation"] == data["generation"]
            assert data["state"]["revision"] == data["revision"]
            self.state = data["state"]
        return data

    def get(self, path, *, expect=200):
        response = self.client.get(path)
        data = self.client.json(response)
        self.bodies.append((path, response.body.decode("utf-8")))
        assert response.code == expect, (path, response.code, data)
        return data

    def fail(self, path, body, code, status=409):
        data = self.post(path, body, expect=status)
        assert data["ok"] is False and data["error"]["code"] == code, data
        return data

    def login(self):
        return self.post("/api/session/login", {})

    def send(self, text, files=0):
        self._ids += 1
        body = {"text": text, "external_files": files, "client_msg_id": f"message-{self._ids:04d}"}
        return self.post("/api/chat/message", body)["rkey"]

    def shortcut(self, key, *, expect=200):
        self._ids += 1
        body = {"shortcut": key, "client_msg_id": f"message-{self._ids:04d}"}
        return self.post("/api/chat/shortcut", body, expect=expect)

    def response(self, rkey):
        return self.state["chat"]["responses"][rkey]

    def task(self, tkey):
        return next(task for task in self.state["device"]["tasks"] if task["key"] == tkey)

    def gesture(self, tkey):
        gesture = self.task(tkey)["gesture"]
        assert gesture is not None, self.task(tkey)["step"]
        return gesture

    def device(self, tkey, op, **extra):
        gesture = self.gesture(tkey)
        assert gesture["op"] == op
        path = {
            "confirm": "confirm",
            "mfa_request": "mfa/request",
            "mfa_verify": "mfa/verify",
        }[op]
        body = {"gesture": gesture["token"], "card_version": gesture["card_version"], **extra}
        return self.post(f"/api/device/tasks/{tkey}/{path}", body)

    def gate(self, rkey):
        return self.response(rkey)["gate"]

    def answer(self, rkey, answers, *, expect=200):
        body = {"gate_version": self.gate(rkey)["version"], "answers": answers}
        return self.post(f"/api/chat/responses/{rkey}/gate/answer", body, expect=expect)

    def pair(self):
        return PAIR.search(self.state["session"]["phone_url"]).group(1)

    def sms_code(self):
        """Read the code where a person would: the simulated phone page's own API."""
        response = self.client.get(f"/api/phone/{self.pair()}?lang=zh-CN&view=customer")
        data = self.client.json(response)
        for message in data["messages"]:
            if message["status"] == "active":
                return CODE.search(message["text"]).group(0)
        return None

    def red(self, tkey):
        self.device(tkey, "confirm")
        self.device(tkey, "mfa_request")
        self.device(tkey, "mfa_verify", code=self.sms_code())
        return self.device(tkey, "confirm")

    def tab(self, name):
        return self.get(f"/api/tabs/{name}")["tab"]["vm"]

    def listed(self, kind):
        """The version of the list a business choice indexes, as the page shows it."""
        if kind == "subscriptions":
            return (self.tab("accounts")["subscriptions"] or {}).get("cancel_version", "0" * 32)
        vm = self.tab("wealth")
        if kind == "wealth":
            return (vm["comparison"] or {}).get("candidates_version", "0" * 32)
        return vm["positions_version"]

    def pick(self, path, choice, *, expect=200):
        kind = {"/api/subscriptions/cancel": "subscriptions", "/api/wealth/purchase": "wealth"}
        body = {"choice": choice, "list_version": self.listed(kind.get(path, "positions"))}
        return self.post(path, body, expect=expect)

    def compensate(self, *, expect=200):
        version = self.tab("operator")["compensation"]["compensate_version"]
        return self.post("/api/chat/compensate", {"plan_version": version}, expect=expect)


@pytest.fixture
def browser(web_app):
    page = Browser(web_app)
    page.login()
    return page


def test_a_message_becomes_the_current_reply_with_its_turns(browser):
    rkey = browser.send(YELLOW)
    chat = browser.state["chat"]
    assert chat["current"] == rkey
    roles = [(turn["role"], turn.get("rkey")) for turn in chat["turns"]]
    assert roles[-2:] == [("user", rkey), ("assistant", rkey)]
    assert browser.response(rkey)["current"] is True
    assert browser.response(rkey)["device_task"] == "T" + rkey
    assert browser.task("T" + rkey)["step"] == "confirm"


def test_shortcuts_follow_the_capabilities(browser, web_app):
    # A build that opens only the P0-core shortcuts (the shipped FEATURES opens ext items).
    web_app.settings["features"] = frozenset({
        "shortcut:transfer",
        "shortcut:cards",
        "shortcut:scam_drill",
    })
    for key in ("transfer", "cards", "scam_drill"):
        assert re.fullmatch(r"R[0-9]+", browser.shortcut(key)["rkey"])
    data = browser.shortcut("billing", expect=404)
    assert data["error"]["code"] == "not_found"


def test_a_client_message_id_replays_or_conflicts(browser, web_app):
    body = {"text": YELLOW, "external_files": 0, "client_msg_id": "same-id-0001"}
    first = browser.post("/api/chat/message", body)["rkey"]
    assert browser.post("/api/chat/message", body)["rkey"] == first
    assert len(browser.ws.transcript) == 2
    conflict = {**body, "text": RED}
    data = browser.fail("/api/chat/message", conflict, "msg_id_conflict")
    assert data["state"]["chat"]["current"] == first


def test_message_text_limits(browser):
    browser.fail(
        "/api/chat/message",
        {"text": " ", "external_files": 0, "client_msg_id": "abcdefgh"},
        "invalid_input",
        422,
    )
    long = {"text": "转" * 4001, "external_files": 0, "client_msg_id": "abcdefgh"}
    browser.fail("/api/chat/message", long, "invalid_input", 422)
    assert browser.ws.transcript == []


def test_before_login_no_message_reaches_the_chat(web_app):
    page = Browser(web_app)
    assert page.state["login"] is not None
    page.fail(
        "/api/chat/message",
        {"text": YELLOW, "external_files": 0, "client_msg_id": "abcdefgh"},
        "not_applicable",
    )
    assert page.ws.transcript == []


def test_older_replies_answer_with_not_current(browser, monkeypatch):
    clarify = browser.send(CLARIFY_PLAIN)
    gated = browser.send(NEW)
    version = browser.gate(gated)["version"]
    browser.send(YELLOW)
    spy = Spy(browser.ws.controller, monkeypatch)
    for path, body in (
        (f"/api/chat/responses/{clarify}/recipient", {"choice": "c0"}),
        (f"/api/chat/responses/{gated}/gate/answer", {"gate_version": version, "answers": SAFE}),
        (f"/api/chat/responses/{gated}/gate/retry", {"gate_version": version}),
    ):
        data = browser.fail(path, body, "not_current")
        assert data["state"]["chat"]["current"] != clarify
    assert spy.total() == 0
    assert browser.task("T" + gated)["step"] == "not_current"
    assert browser.task("T" + gated)["gesture"] is None
    browser.post(f"/api/chat/responses/{gated}/current", {})
    assert browser.state["chat"]["current"] == gated
    browser.fail("/api/chat/responses/R99/current", {}, "not_found", 404)


def test_a_chosen_recipient_shows_one_authoritative_card_in_both_panes(browser):
    rkey = browser.send(CLARIFY)
    assert browser.response(rkey)["device_task"] is None
    browser.post(f"/api/chat/responses/{rkey}/recipient", {"choice": "c0"})
    phone = browser.response(rkey)["cards"]
    device = browser.task("T" + rkey)["cards"]
    strip = lambda cards: [{k: v for k, v in card.items() if k != "key"} for card in cards]  # noqa: E731
    assert phone and strip(phone) == strip(device)
    assert browser.task("T" + rkey)["step"] == "gate_blocked"
    browser.answer(rkey, SAFE)
    assert browser.device("T" + rkey, "confirm")["result"] == {"status": "settled"}
    data = browser.fail(f"/api/chat/responses/{rkey}/recipient", {"choice": "c1"}, "not_applicable")
    assert data["state"]["chat"]["responses"][rkey]["status"] == "settled"


def test_the_recipient_check_over_http(browser):
    rkey = browser.send(FRAUD)
    gate = browser.gate(rkey)
    assert (gate["decision"], gate["interactive"]) == ("incomplete", True)
    assert browser.task("T" + rkey)["gesture"] is None
    browser.answer(rkey, {"verified": "yes", "guided": "yes"})
    paused = browser.gate(rkey)
    assert paused["decision"] == "pause" and paused["locked"] is True
    data = browser.answer(rkey, SAFE, expect=409)
    assert data["error"]["code"] == "not_applicable"
    stale = {"gate_version": paused["version"]}
    browser.post(f"/api/chat/responses/{rkey}/gate/retry", stale)
    browser.fail(f"/api/chat/responses/{rkey}/gate/retry", stale, "gate_stale")
    browser.fail(
        f"/api/chat/responses/{rkey}/gate/answer",
        {"gate_version": paused["version"], "answers": SAFE},
        "gate_stale",
    )
    browser.answer(rkey, SAFE)
    assert browser.gate(rkey)["decision"] == "proceed"
    assert browser.gesture("T" + rkey)["op"] == "confirm"


def test_a_directed_payment_arrives_paused_with_its_preset(browser):
    rkey = browser.send(DIRECTED_NEW)
    gate = browser.gate(rkey)
    assert (gate["decision"], gate["preset"], gate["locked"]) == ("pause", True, True)
    task = browser.task("T" + rkey)
    assert task["step"] == "gate_blocked" and task["gesture"] is None


def test_the_phrase_gate_holds_no_card_and_no_task(browser, monkeypatch):
    spy = Spy(browser.ws.controller, monkeypatch)
    for rkey in (browser.send(HELD), browser.shortcut("scam_drill")["rkey"]):
        response = browser.response(rkey)
        assert response["cards"] == [] and response["device_task"] is None
        assert response.get("phrase_gate_note")
    assert browser.state["device"]["tasks"] == [] and spy.total() == 0


def test_cancel_and_handoff_are_written_into_the_chat(browser):
    yellow = browser.send(YELLOW)
    stop = browser.post("/api/chat/stop", {})["rkey"]
    turns = browser.state["chat"]["turns"]
    assert [turn["role"] for turn in turns[-2:]] == ["user", "assistant"]
    assert turns[-1]["rkey"] == stop == browser.state["chat"]["current"]
    assert browser.response(yellow)["status"] == "withdrawn"
    assert browser.state["device"]["tasks"] == []
    handoff = browser.post("/api/chat/handoff", {})["rkey"]
    assert browser.state["chat"]["current"] == handoff
    assert browser.state["chat"]["turns"][-1]["rkey"] == handoff


def test_switching_language_keeps_the_answers_and_the_token(browser):
    shortcut = browser.shortcut("transfer")["rkey"]
    rkey = browser.send(FRAUD)
    browser.answer(rkey, SAFE)
    gesture = browser.gesture("T" + rkey)
    hint = browser.ws.turn_of(shortcut).hint
    version = browser.gate(rkey)["version"]
    browser.post("/api/session/locale", {"locale": "en"})
    browser.post("/api/session/view", {"view": "customer"})
    after = browser.gesture("T" + rkey)
    assert [after[key] for key in ("op", "token", "card_version")] == [
        gesture[key] for key in ("op", "token", "card_version")
    ]
    assert browser.state["chat"]["responses"][rkey]["gate"]["version"] == version
    assert browser.ws.turn_of(shortcut).hint == hint
    assert browser.device("T" + rkey, "confirm")["result"] == {"status": "waiting_mfa"}


def test_reading_state_changes_nothing(browser, monkeypatch):
    rkey = browser.send(FRAUD)
    browser.answer(rkey, SAFE)
    ws = browser.ws
    spy = Spy(ws.controller, monkeypatch)
    before = (dict(ws.gestures), dict(ws.gates), dict(ws.gate_eval), ws.revision)
    for _ in range(3):
        browser.get("/api/state")
        browser.get("/api/tabs/operator")
        browser.get(f"/api/evidence/responses/{rkey}")
    assert (dict(ws.gestures), dict(ws.gates), dict(ws.gate_eval), ws.revision) == before
    assert spy.total() == 0


PATH = "secret-path C:" + chr(92) + "x"


@pytest.mark.parametrize("failure", [RuntimeError(PATH), TypeError("secret")])
def test_an_unexpected_service_error_shows_no_detail(failure, browser, monkeypatch):
    """§6.5 #14: whatever escapes the service, the body names a code and nothing else."""
    from corridor.web import service

    def broken(*args, **kwargs):
        raise failure

    monkeypatch.setattr(service, "send_message", broken)
    response = browser.client.post(
        "/api/chat/message", {"text": YELLOW, "external_files": 0, "client_msg_id": "abcdefgh"}
    )
    body = response.body.decode("utf-8")
    # R1-E6: the request may have changed state before it failed, so the session is no
    # longer trusted; the answer names the generic outcome code and nothing else.
    data = browser.client.json(response)
    assert response.code == 500 and data["error"]["code"] == "outcome_unknown"
    for leak in ("secret", "C:" + chr(92), "Traceback", "RuntimeError", "TypeError"):
        assert leak not in body
    assert browser.ws.untrusted is True and browser.ws.gestures == {}
