"""B2: customer-view trimming over HTTP (design §6.5 item 13, INV-5.1, 5.2, 5.4).

Every response a customer page receives across the core flows (and the ext tabs, opened
here) is collected from the real handlers and scanned: no rule identifier, 64-digit hex,
UUID, live failure source code, variable name prefix, internal word or ``scripted``, and
no judge-only key (the contract validator reports any). Trimming happens on the server;
the view is a display preference, not access control.
"""

import json
import re

import pytest

import corridor.web as web_package
from corridor.kernel.rules import RULES
from corridor.ui.live_controls import FAILURE_REASONS
from corridor.web import contracts
from tests.unit.test_ui_scam_friction import CLARIFY_PLAIN, FRAUD, SAFE
from tests.unit.test_ui_scam_r7 import DIRECTED_NEW, HELD
from tests.web.conftest import ws_of

pytestmark = pytest.mark.usefixtures("st_sentinel")
YELLOW = "给妈妈转500元"
RED = "给妈妈转5000元"
EXT = frozenset({
    "tab:accounts",
    "tab:plans",
    "tab:wealth",
    "shortcut:billing",
    "shortcut:subscriptions",
    "shortcut:birthday",
    "plan_select",
    "compensate",
})
RULE_PREFIXES = sorted({rule.split("-", 1)[0] for rule in RULES}, key=len, reverse=True)
RULE_ID = re.compile(r"\b(?:" + "|".join(RULE_PREFIXES) + r")-[A-Z0-9][A-Z0-9-]*\b")
LEAKS = {
    "64-digit hex": re.compile(r"[0-9a-f]{64}"),
    "uuid": re.compile(r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}"),
    "variable name": re.compile(r"\b(?:LIVE|LLM|CORRIDOR)_"),
    # The machine word (planner source, class names); the English badge copy may say
    # "Scripted demo", as on the Streamlit page (test_s4_ui checks the same word).
    "scripted": re.compile(r"scripted"),
    "internal word": re.compile("门控|规范卡|签名载荷|审计链|前置条件|展示层"),
    "source code": re.compile("|".join(re.escape(code) for code in FAILURE_REASONS)),
}


class Customer:
    """A customer page: every body it receives is kept for the scan."""

    def __init__(self, client):
        self.client = client
        self.bodies = []

    def keep(self, response, *, view="customer"):
        body = self.client.json(response)
        self.bodies.append((response.request.url, body, view))
        return body

    def post(self, path, body=None):
        return self.keep(self.client.post(path, body or {}))

    def get(self, path, *, view="customer"):
        return self.keep(self.client.get(path), view=view)

    def state(self):
        return self.get("/api/state")["state"]

    def send(self, text, client_msg_id):
        body = {"text": text, "external_files": 0, "client_msg_id": client_msg_id}
        return self.post("/api/chat/message", body)["rkey"]

    def task(self, rkey=None):
        state = self.state()
        rkey = rkey or state["chat"]["current"]
        return next(task for task in state["device"]["tasks"] if task.get("rkey") == rkey)

    def gesture(self, *, code=None):
        task = self.task()
        held = task["gesture"]
        base = f"/api/device/tasks/{task['key']}"
        body = {"gesture": held["token"], "card_version": held["card_version"]}
        path = {"confirm": "/confirm", "mfa_request": "/mfa/request", "mfa_verify": "/mfa/verify"}
        if held["op"] == "mfa_verify":
            body["code"] = code or self.sms_code()
        return self.post(base + path[held["op"]], body)

    def sms_code(self):
        url = self.state()["session"]["phone_url"]
        pair = url.split("/phone/", 1)[1].split("?", 1)[0]
        phone = self.get(f"/api/phone/{pair}?lang=zh-CN&view=customer")
        latest = next(message for message in phone["messages"] if message["latest"])
        return re.search(r"(?<!\d)\d{6}(?!\d)", latest["text"]).group()

    def answer(self, answers):
        state = self.state()
        response = state["chat"]["responses"][state["chat"]["current"]]
        version = response["gate"]["version"]
        path = f"/api/chat/responses/{response['rkey']}/gate/answer"
        return self.post(path, {"gate_version": version, "answers": answers})


def scan(url, body, view):
    text = json.dumps(body, ensure_ascii=False)
    found = [f"{url}: rule id {match}" for match in RULE_ID.findall(text)]
    found += [f"{url}: {name}" for name, pattern in LEAKS.items() if pattern.search(text)]
    found += [f"{url}: {problem}" for problem in contracts.problems(body, view=view)]
    return found


def test_card_term_is_rejected_even_in_explanatory_copy():
    key = "web.ui.login.intro"
    for url, body in (
        ("/api/i18n", {"strings": {key: "规范卡"}}),
        ("/api/i18n", {"strings": {key: "规范卡审计链"}}),
        ("/api/i18n", {"strings": {"other": "规范卡"}}),
        ("/api/state", {key: "规范卡"}),
    ):
        assert any("internal word" in problem for problem in scan(url, body, "customer"))


@pytest.fixture
def customer(client, monkeypatch):
    monkeypatch.setattr(web_package, "FEATURES", web_package.FEATURES | EXT)
    page = Customer(client)
    page.keep(client.post("/api/session", {"view": "customer", "locale": "zh-CN"}))
    page.get("/api/i18n")
    page.post("/api/session/login")
    return page


def test_no_customer_response_carries_judge_evidence_or_internal_words(customer, client):
    page = customer
    page.send(YELLOW, "trim-0001")
    page.gesture()
    rkey = page.send(RED, "trim-0002")
    for _ in range(4):
        page.gesture()
    page.get(f"/api/evidence/responses/{rkey}")
    page.send(FRAUD, "trim-0003")
    page.answer(SAFE)
    page.gesture()
    page.send(DIRECTED_NEW, "trim-0004")
    page.send(HELD, "trim-0005")
    page.post("/api/chat/shortcut", {"shortcut": "scam_drill", "client_msg_id": "trim-0006"})
    clarify = page.send(CLARIFY_PLAIN, "trim-0007")
    page.post(f"/api/chat/responses/{clarify}/recipient", {"choice": "c0"})
    page.gesture()
    page.send("查询余额", "trim-0008")
    page.send("看看9月账单", "trim-0009")
    page.send("有没有异常扣款", "trim-0010")
    page.send("查看我的订阅", "trim-0011")
    page.gesture()
    plan = page.send("帮我准备老婆的生日", "trim-0012")
    task = page.task(plan)
    page.post(
        f"/api/device/tasks/{task['key']}/nodes",
        {"card_version": task["nodes_version"], "nodes": ["n0", "n1", "n2", "n3"]},
    )
    page.gesture()
    page.post("/api/chat/handoff")
    for name in ("operator", "accounts", "plans", "wealth"):
        page.get(f"/api/tabs/{name}")
    page.post("/api/chat/stop")
    page.post("/api/stop")
    page.post("/api/session/locale", {"locale": "en"})
    page.get("/api/i18n")
    page.state()
    evidence = [body for url, body, _view in page.bodies if "/api/evidence/" in url]
    assert evidence == [{"ok": True, **evidence[0]}] and evidence[0]["available"] is False
    assert "technical" not in evidence[0]
    states = [body["state"] for _url, body, _view in page.bodies if "state" in body]
    assert states and all("evidence" not in state for state in states)
    found = [problem for item in page.bodies for problem in scan(*item)]
    assert not found, found[:20]


def test_a_locked_customer_page_names_the_way_out_without_rules(customer):
    page = customer
    page.send(RED, "lock-0001")
    page.gesture()
    page.gesture()
    for _ in range(3):
        code = page.sms_code()
        page.gesture(code=f"{(int(code) + 1) % 1_000_000:06d}")
    state = page.state()
    assert state["shell"]["safety"]["locked"] and state["device"]["tasks"][0]["step"] == (
        "account_locked"
    )
    found = [problem for item in page.bodies for problem in scan(*item)]
    assert not found, found[:20]


def test_the_judge_view_of_the_same_session_does_carry_the_evidence(customer, client, web_app):
    page = customer
    rkey = page.send(RED, "judge-0001")
    page.gesture()
    page.post("/api/session/view", {"view": "judge"})
    state = page.state()
    assert state["evidence"]["consistency"]["same_hash"] is True
    assert RULE_ID.search(json.dumps(state)) or state["evidence"]["kernel"]["rule_ids"] == []
    technical = client.json(client.get(f"/api/evidence/responses/{rkey}"))
    assert technical["available"] is True and technical["technical"]["kernel"]["outcome"]
    contracts.validate_response(technical, view="judge")
    assert ws_of(web_app, client).view == "judge"
