"""S7 on the web over HTTP: the live endpoints through the real handlers (design §2.5.7).

The application is the real one (``make_app``) with the live capability opened for these
tests only; the planner behind it is the test-only fake model, and sockets beyond
loopback are refused. Covered here: the capability gate, the switch-off refusals, the
unlock, switch and deployment POSTs and their boundary rules, wrong-code locks under
concurrency (5 per session, 20 per ledger), GET purity, the reset, and a sentinel scan of
every response body and log line for keys, access codes, wrong codes and proofs.
"""

import json
import logging
import re
import secrets
import threading
from pathlib import Path

import pytest

import corridor.web
from corridor.ui import live_runtime
from corridor.ui.live_config import DEPLOYMENTS
from corridor.ui.live_runtime import JournalClient, access_proof
from tests.unit.test_ui_live_mode import ACCESS_CODE, FAKE_KEY, FakeModel, environ
from tests.unit.test_ui_live_review3 import reachable_text
from tests.web.conftest import WebClient, serve, ws_of

LIVE = corridor.web.FEATURES | {"live"}
LABEL = DEPLOYMENTS[0].label
HMAC_JS = Path(live_runtime.__file__).with_name("live_access") / "hmac.js"
WRONG = "wrong-code-0"


@pytest.fixture
def web_settings(monkeypatch):
    # The routes read the app's features and the view models read the package's: both open.
    monkeypatch.setattr(corridor.web, "FEATURES", LIVE)
    return {"features": LIVE}


@pytest.fixture
def hosted(monkeypatch, tmp_path):
    def apply(**extra):
        for name, value in environ(tmp_path, **extra).items():
            monkeypatch.setenv(name, value)
        return tmp_path / "journal"

    return apply


class Recorded(WebClient):
    """A browser that keeps every response body it received."""

    bodies: list[str]

    def request(self, method, path, body, lines, *, read=True):
        response = super().request(method, path, body, lines, read=read)
        if response is not None:
            self.bodies.append(response.body.decode("utf-8", "replace"))
        return response


def browser(app, view="judge", bodies=None):
    client = Recorded(app)
    client.bodies = bodies if bodies is not None else []
    client.open(view=view)
    assert client.post("/api/session/login", {}).code == 200
    return client


def data(client, response):
    return client.json(response)


def unlock(client, code=ACCESS_CODE):
    challenge = data(client, client.post("/api/live/challenge", {}))
    nonce = challenge["nonce"]
    assert re.fullmatch(r"[0-9a-f]{32}", nonce) and "state" in challenge
    response = client.post("/api/live/unlock", {"nonce": nonce, "proof": access_proof(code, nonce)})
    return response, data(client, response)


def send(client, text):
    body = {"text": text, "external_files": 0, "client_msg_id": secrets.token_hex(8)}
    return client.post("/api/chat/message", body)


def go_live(client):
    response, answer = unlock(client)
    assert response.code == 200 and answer["result"] == "accepted"
    response = client.post("/api/live/toggle", {"on": True})
    assert response.code == 200, data(client, response)
    return data(client, response)


def code_of(client, response):
    return data(client, response)["error"]["code"]


def test_without_the_capability_no_live_route_answers():
    with serve({"features": corridor.web.FEATURES - {"live"}}) as app:
        client = browser(app)
        for path, body in (
            ("/api/live/challenge", {}),
            ("/api/live/unlock", {"nonce": "0" * 32, "proof": "0" * 64}),
            ("/api/live/select", {"deployment": DEPLOYMENTS[1].id}),
            ("/api/live/toggle", {"on": True}),
        ):
            response = client.post(path, body)
            assert response.code == 404 and code_of(client, response) == "not_found"
        assert client.get("/api/live").code == 404
        assert client.get("/vendor/corridor-hmac.js").code == 404
        assert ws_of(app, client).live is None


def test_with_the_switch_off_the_panel_is_off_and_live_posts_are_refused(web_app):
    client = browser(web_app, view="customer")
    panel = data(client, client.get("/api/live"))["live"]
    assert panel["enabled"] is False and panel["off_text"] is None
    assert panel["badge"]["kind"] == "demo"
    assert "price" not in panel and "counters" not in panel
    for path, body in (("/api/live/challenge", {}), ("/api/live/toggle", {"on": True})):
        response = client.post(path, body)
        answer = data(client, response)
        assert response.code == 409 and answer["error"]["code"] == "live_unavailable"
        assert answer["state"]["shell"]["badge"]["kind"] == "demo"
    vendor = client.get("/vendor/corridor-hmac.js")
    assert vendor.code == 200 and vendor.body == HMAC_JS.read_bytes()
    assert ws_of(web_app, client).live is None
    assert data(client, client.get("/api/state"))["state"]["capabilities"]["live"] is True


@pytest.mark.parametrize("deployment_id", ["openai-gpt-6.1-sol", "openai-gpt-5.4-mini"])
def test_unlocking_switching_and_a_live_reply_over_http(
    web_app, hosted, monkeypatch, deployment_id
):
    FakeModel(monkeypatch)
    hosted()
    client = browser(web_app)
    ws = ws_of(web_app, client)
    scripted = ws.controller.client
    panel = data(client, client.get("/api/live"))["live"]
    assert panel["enabled"] is True and panel["toggle"]["disabled"] is True
    assert len(panel["deployments"]) == 6
    assert [item["id"] for item in panel["deployments"]] == [item.id for item in DEPLOYMENTS]
    assert panel["selected"] == "openai-gpt-6.1-sol"
    response = client.post("/api/live/select", {"deployment": deployment_id})
    assert response.code == 200 and data(client, response)["live"]["selected"] == deployment_id
    label = next(item.label for item in DEPLOYMENTS if item.id == deployment_id)
    response, answer = unlock(client, WRONG)
    assert response.code == 200 and answer["result"] == "invalid"
    assert answer["live"]["unlock"]["result_text"] and answer["state"]["revision"] >= 2
    answer = go_live(client)
    assert answer["live"]["badge"] == {
        "kind": "model",
        "text": answer["state"]["shell"]["badge"]["text"],
    }
    assert label in answer["state"]["shell"]["corner"]
    assert label in answer["state"]["shell"]["judge_strip"]
    assert (
        isinstance(ws.controller.client, JournalClient)
        and ws.session.client is ws.controller.client
    )
    response = send(client, "查一下余额")
    state = data(client, response)["state"]
    assert response.code == 200 and state["shell"]["badge"]["kind"] == "model"
    assert ws.op_log[-1]["planner"] == label and ws.op_log[-1]["rkey"] in state["chat"]["responses"]
    rows = state["evidence"]["planner_log"]
    assert rows and rows[-1]["planner"] == label
    response = client.post("/api/live/toggle", {"on": False})
    answer = data(client, response)
    assert response.code == 200 and answer["live"]["badge"]["kind"] == "demo"
    assert ws.controller.client is scripted and ws.session.client is scripted


def test_the_switch_waits_for_the_transaction_boundary_over_http(web_app, hosted, monkeypatch):
    FakeModel(monkeypatch)
    hosted()
    client = browser(web_app)
    ws = ws_of(web_app, client)
    go_live(client)
    assert send(client, "给妈妈转5000元").code == 200
    assert ws.controller.at_transaction_boundary() is False
    nonce = data(client, client.post("/api/live/challenge", {}))["nonce"]
    for path, body in (
        ("/api/live/unlock", {"nonce": nonce, "proof": access_proof(ACCESS_CODE, nonce)}),
        ("/api/live/toggle", {"on": False}),
        ("/api/live/select", {"deployment": DEPLOYMENTS[1].id}),
    ):
        response = client.post(path, body)
        answer = data(client, response)
        assert response.code == 409 and answer["error"]["code"] == "not_at_boundary", path
        assert "state" in answer and answer["state"]["shell"]["badge"]["kind"] == "model"
    # The refused unlock did not use up the challenge.
    assert ws.live.challenge() == nonce
    panel = data(client, client.get("/api/live"))["live"]
    assert panel["boundary"] is False and panel["boundary_text"]
    assert client.post("/api/stop", {}).code == 200
    response = client.post("/api/live/select", {"deployment": DEPLOYMENTS[1].id})
    assert response.code == 409 and code_of(client, response) == "live_on"
    assert client.post("/api/live/toggle", {"on": False}).code == 200
    response = client.post("/api/live/select", {"deployment": "no-such-deployment"})
    assert response.code == 422 and code_of(client, response) == "invalid_input"
    response = client.post("/api/live/select", {"deployment": DEPLOYMENTS[1].id})
    assert response.code == 200 and data(client, response)["live"]["selected"] == DEPLOYMENTS[1].id


def test_malformed_live_bodies_are_refused_before_the_adapter(web_app, hosted):
    hosted()
    client = browser(web_app)
    for path, body, code in (
        ("/api/live/unlock", {"nonce": "x" * 32, "proof": "0" * 64}, "bad_json"),
        ("/api/live/unlock", {"nonce": "0" * 32, "proof": "0" * 63}, "bad_json"),
        ("/api/live/unlock", {"nonce": "0" * 32, "proof": "0" * 64, "code": "1"}, "unknown_field"),
        ("/api/live/unlock", {"code": ACCESS_CODE}, "unknown_field"),
        ("/api/live/toggle", {"on": "yes"}, "bad_json"),
        ("/api/live/select", {"deployment": "../x"}, "bad_json"),
    ):
        response = client.post(path, body)
        assert response.code == 400 and code_of(client, response) == code, (path, body)
    assert ws_of(web_app, client).live is None


def test_wrong_codes_lock_a_session_after_five_and_every_session_after_twenty(web_app, hosted):
    hosted()
    clients = [browser(web_app) for _ in range(4)]
    results: dict[int, list[str]] = {index: [] for index in range(4)}

    def guess(index):
        for _ in range(live_runtime.LOCK_AFTER_FAILURES):
            results[index].append(unlock(clients[index], WRONG)[1]["result"])

    threads = [threading.Thread(target=guess, args=(index,)) for index in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(120)
    assert all(
        found == ["invalid"] * live_runtime.LOCK_AFTER_FAILURES for found in results.values()
    )
    assert unlock(clients[0])[1]["result"] == "locked"
    # Twenty wrong codes on one ledger lock live access for every session, a new one too.
    late = browser(web_app)
    response, answer = unlock(late)
    assert response.code == 200 and answer["result"] == "locked_all"
    panel = data(late, late.get("/api/live"))["live"]
    assert panel["unlock"]["disabled"] is True and panel["toggle"]["disabled"] is True
    assert ws_of(web_app, late).live.snapshot()["reason"] == "locked_all"


def test_reads_change_nothing(web_app, hosted, monkeypatch):
    FakeModel(monkeypatch)
    journal = hosted()
    client = browser(web_app)
    ws = ws_of(web_app, client)
    client_before, revision = ws.controller.client, ws.revision
    for _ in range(3):
        assert client.get("/api/live").code == 200
        assert client.get("/api/state").code == 200
    assert ws.live is None and ws.revision == revision and ws.controller.client is client_before
    assert not list(journal.glob("*/session-*"))
    go_live(client)
    send(client, "查一下余额")
    before = (ws.controller.client, ws.live_toggle, ws.live.snapshot()["calls"], len(ws.op_log))
    revision = ws.revision
    for _ in range(3):
        assert client.get("/api/live").code == 200
        assert client.get("/api/state").code == 200
    after = (ws.controller.client, ws.live_toggle, ws.live.snapshot()["calls"], len(ws.op_log))
    assert after == before and ws.revision == revision


def test_a_reset_keeps_the_live_session_and_turns_the_switch_off(web_app, hosted, monkeypatch):
    FakeModel(monkeypatch)
    hosted()
    client = browser(web_app)
    go_live(client)
    send(client, "查一下余额")
    old = ws_of(web_app, client)
    session, calls = old.live, old.live.snapshot()["calls"]
    response = client.post("/api/session/reset", {})
    assert response.code == 200
    new = ws_of(web_app, client)
    assert new is not old and new.live is session and new.live_toggle is False
    assert session.snapshot()["calls"] == calls
    assert data(client, response)["state"]["shell"]["badge"]["kind"] == "demo"
    assert new.controller.client is new.scripted_client


def test_no_key_code_or_proof_reaches_a_response_or_a_log(web_app, hosted, monkeypatch, caplog):
    FakeModel(monkeypatch)
    hosted()
    caplog.set_level(logging.DEBUG)
    bodies: list[str] = []
    proofs = []
    for view in ("customer", "judge"):
        client = browser(web_app, view=view, bodies=bodies)
        for code in (WRONG, ACCESS_CODE):
            challenge = data(client, client.post("/api/live/challenge", {}))
            proofs.append(access_proof(code, challenge["nonce"]))
            client.post("/api/live/unlock", {"nonce": challenge["nonce"], "proof": proofs[-1]})
        client.post("/api/live/toggle", {"on": True})
        send(client, "给妈妈转800元")
        client.get("/api/live")
        client.post("/api/stop", {})
        found = reachable_text(ws_of(web_app, client))
        for secret in (FAKE_KEY, ACCESS_CODE, WRONG, *proofs):
            assert not [text for text in found if secret in text]
    logs = "\n".join(record.getMessage() for record in caplog.records)
    for secret in (FAKE_KEY, ACCESS_CODE, WRONG, *proofs):
        assert all(secret not in body for body in bodies)
        assert secret not in logs
    customer = [json.loads(body) for body in bodies[: len(bodies) // 2]]
    for answer in customer:
        panel = answer.get("live") or {}
        assert "price" not in panel and "counters" not in panel
        assert all("credential_text" not in item for item in panel.get("deployments", []))
        text = json.dumps(answer, ensure_ascii=False)
        for word in ("LIVE_", "LLM_", "CORRIDOR_", '"scripted"'):
            assert word not in text


def test_every_live_response_has_the_contract_shape(web_app, hosted, monkeypatch):
    from corridor.web import contracts

    def checked(body, view):
        # select and toggle answer the state and the panel without a result (§3.7, S-3).
        return contracts.problems(body, view=view)

    FakeModel(monkeypatch)
    hosted()
    for view in ("customer", "judge"):
        bodies: list[str] = []
        client = browser(web_app, view=view, bodies=bodies)
        client.get("/api/live")
        unlock(client, WRONG)
        go_live(client)
        send(client, "查一下余额")
        client.get("/api/live")
        client.post("/api/live/toggle", {"on": False})
        client.post("/api/live/select", {"deployment": DEPLOYMENTS[1].id})
        client.post("/api/live/toggle", {"on": True})
        found = [problem for body in bodies for problem in checked(json.loads(body), view)]
        assert not found, found[:10]


# --- ext items in live mode: the boundary and the scripted advisor (INV-10.5, 10.8) -----------

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


def state_of(client):
    return data(client, client.get("/api/state"))["state"]


def gesture(client, task):
    held = task["gesture"]
    assert held is not None, task["step"]
    base = f"/api/device/tasks/{task['key']}"
    path = {"confirm": "/confirm", "mfa_request": "/mfa/request"}[held["op"]]
    body = {"gesture": held["token"], "card_version": held["card_version"]}
    return client.post(base + path, body)


def held(client, ws):
    """The panel and the live POSTs follow the controller's own transaction boundary."""
    boundary = ws.controller.at_transaction_boundary()
    panel = data(client, client.get("/api/live"))["live"]
    assert panel["boundary"] is boundary
    if not boundary:
        assert panel["toggle"]["disabled"] is True and panel["unlock"]["disabled"] is True
        for path, body in (
            ("/api/live/toggle", {"on": False}),
            ("/api/live/select", {"deployment": "x"}),
        ):
            response = client.post(path, body)
            assert response.code == 409 and code_of(client, response) == "not_at_boundary", path
    assert isinstance(ws.controller.client, JournalClient)
    return boundary


def test_ext_flows_keep_the_boundary_rule_and_the_scripted_advisor(web_app, hosted, monkeypatch):
    monkeypatch.setattr(corridor.web, "FEATURES", LIVE | EXT)
    web_app.settings["features"] = LIVE | EXT
    FakeModel(monkeypatch)
    hosted()
    client = browser(web_app)
    ws = ws_of(web_app, client)
    advisor = ws.wealth.client
    go_live(client)
    # Bills: a green query settles at once; the reply names the live planner.
    assert send(client, "看看9月账单").code == 200
    assert ws.op_log[-1]["planner"] == LABEL and held(client, ws) is True
    # Subscriptions: the yellow card holds the boundary until it is confirmed.
    rkey = data(client, send(client, "查看我的订阅"))["rkey"]
    assert ws.op_log[-1]["planner"] == LABEL and held(client, ws) is False
    task = next(item for item in state_of(client)["device"]["tasks"] if item.get("rkey") == rkey)
    assert gesture(client, task).code == 200
    assert held(client, ws) is True
    # Birthday plan and a stop: the switch follows whatever the controller holds open.
    assert send(client, "帮我准备老婆的生日").code == 200
    assert ws.op_log[-1]["planner"] == LABEL
    held(client, ws)
    assert client.post("/api/stop", {}).code == 200
    assert held(client, ws) is True
    # Wealth page: its card holds the boundary; the advisor and the presets stay scripted.
    boundaries = []
    compared = client.post("/api/wealth/compare", {"objective": "net_return"})
    assert compared.code == 200
    boundaries.append(held(client, ws))
    version = data(client, compared)["tab"]["vm"]["comparison"]["candidates_version"]
    body = {"choice": None, "list_version": version}
    assert client.post("/api/wealth/purchase", body).code == 200
    boundaries.append(held(client, ws))
    assert boundaries == [True, False]
    assert client.post("/api/stop", {}).code == 200
    assert client.post("/api/presets/suitability-r4", {}).code == 200
    for demo in ws.demos.values():
        controller = demo.get("controller") if isinstance(demo, dict) else None
        if controller is not None:
            assert controller.client.mode == "scripted"
    assert ws.wealth.client is advisor and advisor.mode == "scripted"
    assert held(client, ws) is True
    # Back at the boundary the switch works again.
    response = client.post("/api/live/toggle", {"on": False})
    assert response.code == 200 and ws.controller.client is ws.scripted_client


def test_a_birthday_plan_and_its_refund_run_in_live_mode(web_app, hosted, monkeypatch):
    web_app.settings["features"] = LIVE | EXT
    FakeModel(monkeypatch)
    hosted()
    client = browser(web_app)
    ws = ws_of(web_app, client)
    go_live(client)
    rkey = data(client, send(client, "帮我准备老婆的生日"))["rkey"]
    task = next(item for item in state_of(client)["device"]["tasks"] if item.get("rkey") == rkey)
    nodes = [f"n{index}" for index in range(task["selection"]["total"])]
    path = f"/api/device/tasks/{task['key']}/nodes"
    assert client.post(path, {"card_version": task["nodes_version"], "nodes": nodes}).code == 200
    task = next(item for item in state_of(client)["device"]["tasks"] if item["key"] == task["key"])
    assert task["step"] == "confirm_plan" and gesture(client, task).code == 200
    assert ws.controller.gateway.bank.execute_count > 0
    # Each operation on this reply is logged with the planner that served it.
    planners = [entry["planner"] for entry in ws.op_log if entry["rkey"] == rkey]
    assert planners and planners[0] == LABEL
    assert all(planner in {LABEL, "scripted"} for planner in planners)
    assert send(client, "停止").code == 200
    refund = data(client, send(client, "退回锁定的预算"))["rkey"]
    assert held(client, ws) is False
    task = next(item for item in state_of(client)["device"]["tasks"] if item.get("rkey") == refund)
    assert gesture(client, task).code == 200
    assert held(client, ws) is True
    assert not ws.failures
    assert isinstance(ws.controller.client, JournalClient)


def sms_code(client):
    url = state_of(client)["session"]["phone_url"]
    pair = url.split("/phone/", 1)[1].split("?", 1)[0]
    phone = data(client, client.get(f"/api/phone/{pair}?lang=zh-CN&view=customer"))
    latest = next(message for message in phone["messages"] if message["latest"])
    return re.search(r"(?<!\d)\d{6}(?!\d)", latest["text"]).group()


def current_task(client, rkey):
    return next(item for item in state_of(client)["device"]["tasks"] if item.get("rkey") == rkey)


def test_live_proposals_still_meet_the_device_the_sms_and_the_recipient_check(
    web_app, hosted, monkeypatch
):
    from corridor.sim.device import Device
    from tests.unit.test_ui_scam_friction import NEW

    calls = []
    original = Device.complete_mfa

    def counted(self, *args, **kwargs):
        calls.append(kwargs.get("code"))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Device, "complete_mfa", counted)
    FakeModel(monkeypatch)
    hosted()
    client = browser(web_app)
    ws = ws_of(web_app, client)
    go_live(client)
    # A red transfer proposed by the live planner: confirm, SMS code, confirm again.
    rkey = data(client, send(client, "给妈妈转5000元"))["rkey"]
    assert ws.op_log[-1]["planner"] == LABEL
    steps = []
    for _ in range(4):
        task = current_task(client, rkey)
        steps.append(task["step"])
        if task["step"] == "mfa_code":
            held_gesture = task["gesture"]
            body = {
                "gesture": held_gesture["token"],
                "card_version": held_gesture["card_version"],
                "code": sms_code(client),
            }
            response = client.post(f"/api/device/tasks/{task['key']}/mfa/verify", body)
        else:
            response = gesture(client, task)
        assert response.code == 200, (task["step"], data(client, response))
    assert steps == ["confirm", "mfa_request", "mfa_code", "final_confirm"]
    assert ws.controller.gateway.bank.execute_count == 1 and calls == []
    assert isinstance(ws.controller.client, JournalClient)
    # A new payee from the live planner meets the same two questions: no gesture before.
    rkey = data(client, send(client, NEW))["rkey"]
    state = state_of(client)
    assert state["chat"]["responses"][rkey]["gate"] is not None
    task = current_task(client, rkey)
    assert task["step"] == "gate_blocked" and task["gesture"] is None
    assert ws.controller.gateway.bank.execute_count == 1
