"""Two drivers for the same flow DSL (design §3.5.1).

``StreamlitDriver`` runs the real ``ui/app.py`` under AppTest with the widget keys of the
S7 flow dump. ``WebDriver`` serves ``corridor.web`` on a
loopback port and sends the contract's HTTP requests (``WEB-CONTRACT.md`` §3), taking
gesture tokens, card versions and gate versions only from the AppState it receives and
the SMS code only from the simulated phone endpoint. Both observe the server-side
controller and session with ``state_probe.observe_state``.
"""

import asyncio
import http.client
import json
import re
import threading
from http.cookies import SimpleCookie
from urllib.parse import urlsplit

from tests.web.state_probe import observe_state

PREFIX = "domestic"
CODE = re.compile(r"(?<!\d)\d{6}(?!\d)")


class DriverError(RuntimeError):
    """A gesture the flow asks for is not offered to the user at this point."""


def wrong_code(code):
    return f"{(int(code) + 1) % 1_000_000:06d}"


class CallProbe:
    """Counts of the S6 ``Spy`` calls (MFA, approval, device signature, dispatch), by path.

    ``controller.complete_mfa(auto)`` counts calls without a typed code on an action
    that is not yet verified: the simulated device would read the code itself
    (INV-2.2). A call without a code on an already verified action (the session's
    verify step after a typed code, in both front ends) returns at once and is counted
    under ``controller.complete_mfa(no_code)`` only.
    """

    def __init__(self, controller):
        from tests.unit.test_ui_scam_friction import SPIED

        self.counts = {}
        for path, name in SPIED:
            target = controller
            for part in path.split("."):
                target = getattr(target, part)
            self._wrap(target, name, f"{path}.{name}")
        original = controller.complete_mfa

        def complete_mfa(pending_id, *, code=None):
            if code is None:
                self._count("controller.complete_mfa(no_code)")
                if not (controller._pending.get(pending_id) or {}).get("verified"):
                    self._count("controller.complete_mfa(auto)")
            return original(pending_id, code=code)

        controller.complete_mfa = complete_mfa

    def _count(self, key):
        self.counts[key] = self.counts.get(key, 0) + 1

    def _wrap(self, target, name, key):
        original = getattr(target, name)

        def wrapper(*args, **kwargs):
            self._count(key)
            return original(*args, **kwargs)

        setattr(target, name, wrapper)

    def snapshot(self):
        return dict(sorted(self.counts.items()))


class StreamlitDriver:
    name = "streamlit"

    def __init__(self, *, features=()):
        from tests.e2e.test_demo_ui import app

        self.page = app()
        self._check("start")
        self.page.button(key="enter").click().run()
        self._settle("enter")
        self.rid = None

    # --- page access -------------------------------------------------------------------

    @property
    def controller(self):
        return self.page.session_state["controller"]

    @property
    def session(self):
        return self.page.session_state["domestic_session"]

    def _state(self, key, default=None):
        state = self.page.session_state
        return state[key] if key in state else default

    def _local(self):
        if self.rid is None:
            return None
        return self._state(f"{PREFIX}_selected:{self.rid}")

    def _identity(self):
        local = self._local() or {}
        payload = self.session.response(self.rid)["payload"]
        return (
            local.get("pending_id")
            or local.get("draft_id")
            or payload.get("pending_id")
            or payload.get("draft_id")
        )

    def _check(self, what):
        if self.page.exception:
            raise RuntimeError(
                f"page exception after {what}: "
                + "; ".join(str(item.value) for item in self.page.exception)
            )

    def _settle(self, what):
        from tests.unit.test_ui_scam_friction import prune

        self._check(what)
        prune(self.page)

    def _keys(self):
        return {button.key for button in self.page.button}

    def _click(self, key):
        if key not in self._keys():
            raise DriverError(f"button {key} is not on the page")
        self.page.button(key=key).click().run()
        self._settle(key)

    def _chat_page(self):
        if not any(item.key == f"{PREFIX}_text" for item in self.page.text_input):
            self.page.selectbox(key="page").select("conversation").run()
            self._settle("navigate")

    # --- gestures ----------------------------------------------------------------------

    def send(self, text):
        self._chat_page()
        self.page.text_input(key=f"{PREFIX}_text").set_value(text)
        self.page.button(key=f"{PREFIX}_send").click().run()
        self._settle("send")
        self.rid = self._state(f"{PREFIX}_response_id")

    def shortcut(self, key):
        self._chat_page()
        self._click(f"{PREFIX}_example_{key}")
        self.rid = self._state(f"{PREFIX}_response_id")

    def answer(self, answers):
        from tests.unit.test_ui_scam_friction import gate_keys

        keys = gate_keys(self.page, PREFIX, self.rid)
        if set(keys) != set(answers):
            raise DriverError(f"recipient check questions {sorted(keys)} are not on the page")
        for question, value in answers.items():
            self.page.radio(key=keys[question]).set_value(value)
        self.page.run()
        self._settle("answer")

    def retry(self):
        keys = [key for key in self._keys() if key.endswith(":retry")]
        if len(keys) != 1:
            raise DriverError(f"answer-again buttons on the page: {keys}")
        self._click(keys[0])

    def select(self, contact):
        self.page.selectbox(key=f"{PREFIX}_clarify_contact").select(contact).run()
        self._settle("choose")
        self._click(f"{PREFIX}_select_contact")

    def subscription_choice(self, choice):
        refs = self.session.response(self.rid)["payload"]["subscription_choices"]
        ref = refs[int(choice[1:])]
        self._click(f"{PREFIX}_subscription:{self.rid}:{ref}")
        self.rid = self._state(f"{PREFIX}_response_id")

    def confirm(self):
        self._click(f"{PREFIX}_selected_confirm" if self._local() else f"{PREFIX}_confirm")

    def _mfa_key(self):
        return f"{PREFIX}_selected_mfa" if self._local() else f"{PREFIX}_verify_mfa"

    def mfa_request(self):
        self._click(f"{self._mfa_key()}:request")

    def _type_code(self, code):
        key = self._mfa_key()
        if not any(item.key == f"{key}:code" for item in self.page.text_input):
            raise DriverError("the code field is not on the page")
        self.page.text_input(key=f"{key}:code").set_value(code)
        self.page.button(key=f"{key}:verify").click().run()
        self._settle("verify")

    def _sms_code(self):
        from tests.e2e.test_demo_ui import phone_inbox_codes

        pending = self._identity()
        expected = self.controller.gateway.mfa.test_code(
            self.controller._pending[pending]["challenge_id"]
        )
        codes = phone_inbox_codes(self.page)
        if expected not in codes:
            raise DriverError("the simulated phone has no active SMS with this code")
        return expected

    def mfa_verify(self):
        self._type_code(self._sms_code())

    def mfa_wrong(self):
        self._type_code(wrong_code(self._sms_code()))

    def cancel(self):
        self._click(f"{PREFIX}_selected_cancel" if self._local() else f"{PREFIX}_cancel")

    def plan(self, nodes):
        payload = self.session.response(self.rid)["payload"]
        shown = payload.get("replacement", payload).get("nodes") or []
        if not shown:
            raise DriverError("no plan nodes on the page")
        wanted = range(len(shown)) if nodes == "all" else nodes
        for index, node in enumerate(shown):
            key = f"{PREFIX}:node:{self.rid}:{node['id']}"
            self.page.checkbox(key=key).set_value(index in wanted)
        self._click(f"{PREFIX}_confirm")

    def handoff(self):
        self._chat_page()
        self._click(f"{PREFIX}_task_handoff")
        self.rid = self._state(f"{PREFIX}_response_id")

    def compensate(self):
        self._chat_page()
        self._click(f"{PREFIX}_task_back")

    def confirm_compensation(self):
        self._click(f"{PREFIX}_compensate_confirm")

    def stop_all(self):
        self._click("stop_all")

    def locale(self, value):
        self.page.selectbox(key="locale").select(value).run()
        self._settle("locale")

    def observe(self):
        return observe_state(self.controller, self.session, self.rid, self._local())

    def close(self):
        pass


class WebDriver:
    name = "web"

    def __init__(self, *, view="judge", locale="zh-CN", features=()):
        """``features`` opens ext items as a build whose ``FEATURES`` lists them would:
        both the endpoints and the AppState capabilities read the module constant."""
        from tornado.testing import bind_unused_port

        import corridor.web

        self._shipped = corridor.web.FEATURES
        corridor.web.FEATURES = self._shipped | frozenset(features)
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()
        sock, self.port = bind_unused_port()
        self.app = self._call(self._start(sock))
        self.store = self.app.settings["store"]
        self.task_source = None
        self.browser = self.tab = self.generation = None
        self.state = None
        self.rkey = self.rid = None
        self._messages = 0
        opened = self.post("/api/session", {"view": view, "locale": locale}, opening=True)
        self.tab = opened["tab"]
        self.post("/api/session/login", {})

    async def _start(self, sock):
        from tornado.httpserver import HTTPServer

        from corridor.web.app import make_app

        app = make_app({"port": self.port})
        self._server = HTTPServer(app)
        self._server.add_sockets([sock])
        return app

    def _call(self, coroutine):
        return asyncio.run_coroutine_threadsafe(coroutine, self._loop).result(timeout=120)

    # --- HTTP --------------------------------------------------------------------------

    def _request(self, method, path, body=None, *, opening=False):
        host = f"127.0.0.1:{self.port}"
        headers = {"Host": host}
        if method == "POST":
            headers.update({"Origin": f"http://{host}", "Content-Type": "application/json"})
        if opening:
            headers["X-Corridor-Web"] = "1"
        elif not path.startswith("/api/phone/"):
            headers.update({"X-Corridor-Tab": self.tab, "X-Corridor-Generation": self.generation})
        if self.browser:
            headers["Cookie"] = f"corridor_web={self.browser}"
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=120)
        try:
            connection.request(method, path, body=data, headers=headers)
            response = connection.getresponse()
            raw = response.read()
            cookie = response.getheader("Set-Cookie")
        finally:
            connection.close()
        if cookie:
            jar = SimpleCookie()
            jar.load(cookie)
            if "corridor_web" in jar:
                self.browser = jar["corridor_web"].value
        payload = json.loads(raw.decode("utf-8"))
        if "state" in payload:
            self.state = payload["state"]
        if "generation" in payload:
            self.generation = payload["generation"]
        return response.status, payload

    def post(self, path, body, *, opening=False):
        status, payload = self._request("POST", path, body, opening=opening)
        if status != 200 or not payload.get("ok"):
            raise DriverError(f"POST {path} -> {status} {payload.get('error')}")
        return payload

    def get(self, path):
        status, payload = self._request("GET", path)
        if status != 200:
            raise DriverError(f"GET {path} -> {status} {payload.get('error')}")
        return payload

    # --- server-side objects -----------------------------------------------------------

    @property
    def ws(self):
        ws = self.store.lookup(self.browser, self.tab)
        if ws is None:
            raise DriverError("the session store has no session for this browser and tab")
        return ws

    @property
    def controller(self):
        return self.ws.controller

    def _response_vm(self):
        return self.state["chat"]["responses"][self.rkey]

    def _task(self):
        """The reply's device task, or after a business page action its newest task."""
        tasks = self.state["device"]["tasks"]
        if self.task_source:
            found = [task for task in tasks if task["source"] == self.task_source]
            if not found:
                raise DriverError(f"no {self.task_source} task on the device")
            return found[0]
        tkey = self._response_vm().get("device_task")
        for task in tasks:
            if task["key"] == tkey:
                return task
        raise DriverError(f"no device task for {self.rkey}")

    def _gesture(self, op, path, extra=None):
        task = self._task()
        gesture = task.get("gesture")
        if not gesture or gesture["op"] != op:
            raise DriverError(f"no {op} gesture: device step is {task['step']}")
        body = {"gesture": gesture["token"], "card_version": gesture["card_version"]}
        self.post(f"/api/device/tasks/{task['key']}/{path}", {**body, **(extra or {})})

    def _gate_version(self):
        gate = self._response_vm().get("gate")
        if not gate:
            raise DriverError(f"no recipient check on {self.rkey}")
        return gate["version"]

    def _subject(self, rkey):
        self.rkey = rkey
        self.rid = self.ws.rkeys[rkey]

    def _message_id(self):
        self._messages += 1
        return f"flow-{self._messages:04d}"

    # --- gestures ----------------------------------------------------------------------

    def send(self, text):
        body = {"text": text, "external_files": 0, "client_msg_id": self._message_id()}
        self._subject(self.post("/api/chat/message", body)["rkey"])

    def shortcut(self, key):
        body = {"shortcut": key, "client_msg_id": self._message_id()}
        self._subject(self.post("/api/chat/shortcut", body)["rkey"])

    def answer(self, answers):
        body = {"gate_version": self._gate_version(), "answers": answers}
        self.post(f"/api/chat/responses/{self.rkey}/gate/answer", body)

    def retry(self):
        body = {"gate_version": self._gate_version()}
        self.post(f"/api/chat/responses/{self.rkey}/gate/retry", body)

    def select(self, contact):
        contacts = self.ws.session.response(self.rid)["payload"].get("contacts") or []
        clarify = self._response_vm().get("clarify") or {}
        choice = f"c{contacts.index(contact)}"
        if choice not in {item["key"] for item in clarify.get("choices", [])}:
            raise DriverError(f"choice {choice} is not offered on {self.rkey}")
        self.post(f"/api/chat/responses/{self.rkey}/recipient", {"choice": choice})

    def subscription_choice(self, choice):
        shown = self._response_vm().get("subscription_select")
        if not shown or choice not in {item["key"] for item in shown["choices"]}:
            raise DriverError(f"subscription choice {choice} is not offered on {self.rkey}")
        result = self.post(
            f"/api/chat/responses/{self.rkey}/subscription",
            {
                "choice": choice,
                "list_version": shown["list_version"],
            },
        )
        self._subject(result["rkey"])

    def confirm(self):
        self._gesture("confirm", "confirm")

    def mfa_request(self):
        self._gesture("mfa_request", "mfa/request")

    def _sms_code(self):
        controller = self.ws.controller
        identity = self.ws.tasks[self._task()["key"]].identity
        expected = controller.gateway.mfa.test_code(controller._pending[identity]["challenge_id"])
        pair = urlsplit(self.state["session"]["phone_url"]).path.rsplit("/", 1)[-1]
        phone = self.get(f"/api/phone/{pair}?lang=zh-CN&view=customer")
        active = [item for item in phone["messages"] if item["status"] == "active"]
        codes = [code for item in active for code in CODE.findall(item["text"])]
        if expected not in codes:
            raise DriverError("the simulated phone has no active SMS with this code")
        return expected

    def mfa_verify(self):
        self._gesture("mfa_verify", "mfa/verify", {"code": self._sms_code()})

    def mfa_wrong(self):
        self._gesture("mfa_verify", "mfa/verify", {"code": wrong_code(self._sms_code())})

    def cancel(self):
        self.post(f"/api/device/tasks/{self._task()['key']}/cancel", {})

    def plan(self, nodes):
        select = self._response_vm().get("plan_select")
        if not select:
            raise DriverError(f"no plan selection on {self.rkey}")
        keys = [node["key"] for node in select["nodes"]]
        chosen = keys if nodes == "all" else [keys[index] for index in nodes]
        task = self._task()
        body = {"card_version": task["nodes_version"], "nodes": chosen}
        self.post(f"/api/device/tasks/{task['key']}/nodes", body)
        gesture = self._task().get("gesture")
        if gesture and gesture["op"] == "confirm":
            self._gesture("confirm", "confirm")

    def wealth_compare(self, objective):
        self.post("/api/wealth/compare", {"objective": objective})

    def _tab(self, name):
        return self.get(f"/api/tabs/{name}")["tab"]["vm"]

    def wealth_purchase(self, choice=None):
        # The page sends the version of the comparison it shows (web-contract/1.1).
        version = self._tab("wealth")["comparison"]["candidates_version"]
        self.post("/api/wealth/purchase", {"choice": choice, "list_version": version})
        self.task_source = "wealth"

    def preset_r4(self):
        self.post("/api/presets/suitability-r4", {})

    def subs_query(self):
        self.post("/api/subscriptions/query", {})
        self.task_source = "subscriptions"

    def subs_cancel(self, choice):
        version = self._tab("accounts")["subscriptions"]["cancel_version"]
        self.post("/api/subscriptions/cancel", {"choice": choice, "list_version": version})
        self.task_source = "subscriptions"

    def handoff(self):
        self._subject(self.post("/api/chat/handoff", {})["rkey"])

    def compensate(self):
        version = self.state["chat"]["task_panel"]["controls"]["compensate_version"]
        self.post("/api/chat/compensate", {"plan_version": version})

    def confirm_compensation(self):
        tasks = [task for task in self.state["device"]["tasks"] if task["source"] == "compensation"]
        if not tasks:
            raise DriverError("no compensation task on the device")
        gesture = tasks[0].get("gesture")
        if not gesture or gesture["op"] != "confirm":
            raise DriverError(f"no confirm gesture: device step is {tasks[0]['step']}")
        body = {"gesture": gesture["token"], "card_version": gesture["card_version"]}
        self.post(f"/api/device/tasks/{tasks[0]['key']}/confirm", body)

    def stop_all(self):
        self.post("/api/stop", {})

    def locale(self, value):
        self.post("/api/session/locale", {"locale": value})

    def observe(self):
        ws = self.ws
        return observe_state(ws.controller, ws.session, self.rid, ws.local.get(self.rid))

    def close(self):
        import corridor.web

        async def stop():
            self._server.stop()

        try:
            self._call(stop())
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=10)
            executor = self.app.settings.get("executor")
            if executor is not None:
                executor.shutdown()
            corridor.web.FEATURES = self._shipped


def run_step(driver, step):
    if step.arg is None:
        getattr(driver, step.op)()
    else:
        getattr(driver, step.op)(step.arg)
