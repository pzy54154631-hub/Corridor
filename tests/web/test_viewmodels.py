"""B2: the page as data for the core flows (design §3.4, §6.1).

Sessions are driven the way the handlers drive them: the service call in the session's
locale and audience, the revision bump, then ``refresh``; the page is ``vm.app_state``,
and every page this suite reads must conform to web-contract/1. ``Web`` is shared by
the other B2 test modules.
"""

import json
import re
from itertools import count

import pytest

from corridor.web import catalog, contracts, refresh, service, vm
from corridor.web.compat import H, reset_audience, scam_friction, set_audience, tr
from corridor.web.state import new_web_session
from tests.unit.test_ui_scam_friction import CLARIFY_PLAIN, FRAUD, NEW, SAFE, snapshot
from tests.unit.test_ui_scam_r7 import DIRECTED_NEW, HELD, OPENING

YELLOW = "给妈妈转500元"
RED = "给妈妈转5000元"
BALANCE = "查询余额"
GUIDED = {"verified": "yes", "guided": "yes"}
OPS = {"confirm": "device_confirm", "mfa_request": "device_mfa_request"}
# Every corridor.ui module's ``st`` raises on use (design §6.7 item 3).
pytestmark = pytest.mark.usefixtures("st_sentinel")
CORE = frozenset({"shortcut:transfer", "shortcut:cards", "shortcut:scam_drill"})


@pytest.fixture
def core_only(monkeypatch):
    """A build that opens only the P0-core shortcuts (the shipped FEATURES opens ext items)."""
    import corridor.web as web_package

    monkeypatch.setattr(web_package, "FEATURES", CORE)


class Web:
    """One web session driven through the service layer, as the handlers do."""

    def __init__(self, *, view="customer", locale="zh-CN"):
        self.ws = new_web_session(view, locale, browser_id="b" * 43, tab_id="t" * 32)
        self._ids = count(1)

    @property
    def controller(self):
        return self.ws.controller

    def post(self, name, *args):
        ws = self.ws
        try:
            with catalog.bound(ws):
                return getattr(service, name)(ws, *args)
        finally:
            ws.revision += 1
            with catalog.bound(ws):
                refresh.refresh(ws)

    def state(self):
        state = vm.app_state(self.ws)
        contracts.validate_state(state)
        return state

    def login(self):
        self.post("login")
        return self

    def send(self, text, files=0):
        return self.post("send_message", text, files, f"msg-{next(self._ids):04d}")

    def shortcut(self, key):
        return self.post("send_shortcut", key, f"msg-{next(self._ids):04d}")

    def response(self, rkey=None):
        state = self.state()
        return state["chat"]["responses"][rkey or state["chat"]["current"]]

    def task(self, rkey=None):
        state = self.state()
        rkey = rkey or state["chat"]["current"]
        return next(task for task in state["device"]["tasks"] if task.get("rkey") == rkey)

    def code(self, tkey):
        pending = self.controller._pending[self.ws.tasks[tkey].identity]
        return self.controller.gateway.mfa.test_code(pending["challenge_id"])

    def gesture(self, rkey=None, *, code=None):
        task = self.task(rkey)
        held = task["gesture"]
        assert held is not None, task["step"]
        args = [task["key"], held["token"], held["card_version"]]
        if held["op"] == "mfa_verify":
            return self.post("device_mfa_verify", *args, code or self.code(task["key"]))
        return self.post(OPS[held["op"]], *args)

    def answer(self, answers, rkey=None):
        response = self.response(rkey)
        return self.post("gate_answer", response["rkey"], response["gate"]["version"], answers)

    def retry(self, rkey=None):
        response = self.response(rkey)
        return self.post("gate_retry", response["rkey"], response["gate"]["version"])

    def red_until_code(self, text=RED):
        self.send(text)
        self.gesture()
        self.gesture()
        return self.task()

    def switch(self, *, view=None, locale=None):
        if view:
            self.post("set_view", view)
        if locale:
            self.post("set_locale", locale)


def without_key(cards):
    return [{key: value for key, value in card.items() if key != "key"} for card in cards]


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def test_the_login_page_has_no_turns_tasks_or_evidence(core_only):
    page = Web().state()
    assert page["login"]["button"] == tr("Enter synthetic demo")
    assert page["chat"]["turns"] == [] and page["device"]["tasks"] == []
    assert page["chat"]["greeting"] and page["device"]["active"] is None
    assert [item["key"] for item in page["chat"]["shortcuts"]] == [
        "transfer",
        "cards",
        "scam_drill",
    ]
    assert "evidence" not in page and "judge_strip" not in page["shell"]
    assert page["session"]["phone_url"].startswith("/phone/")


def test_a_yellow_reply_and_its_device_task_show_the_same_authoritative_card():
    web = Web().login()
    rkey = web.send(YELLOW)
    page = web.state()
    response = page["chat"]["responses"][rkey]
    task = page["device"]["tasks"][0]
    assert task["key"] == response["device_task"] == "T" + rkey
    assert [card["key"] for card in response["cards"]] == [f"{rkey}-0"]
    assert [card["key"] for card in task["cards"]] == [f"T{rkey}-0"]
    assert without_key(response["cards"]) == without_key(task["cards"])
    assert task["step"] == "confirm" and task["gesture"]["op"] == "confirm"
    assert task["gesture"]["label"] == tr("Confirm")
    assert response["cancel"]["label"] == tr("Cancel")
    web.gesture()
    done = web.task(rkey)
    assert done["step"] == "done" and done["gesture"] is None and done["cancel"] is None
    result = web.response(rkey)["result"]
    assert result["banner"] == {"tone": "success", "text": "已办理"}
    assert re.search(r"[0-9A-F]{8}", result["receipt_line"])


def test_cards_are_projected_from_the_authoritative_card_never_the_envelope_copy():
    web = Web(view="judge").login()
    rkey = web.send(YELLOW)
    rid = web.ws.rkeys[rkey]
    stored = web.ws.session._responses[rid]
    stored["card"]["amount"] = "1/1"
    stored["payload"]["card"]["amount"] = "1/1"
    web.post("set_locale", "zh-CN")
    response = web.response(rkey)
    assert response["cards"][0]["amount_text"] == "¥500.00"
    assert response["card_source"] == "authoritative"
    assert response["card_mismatch"]
    assert web.task(rkey)["cards"][0]["amount_text"] == "¥500.00"


def test_the_red_path_names_each_step_and_the_code_stays_on_the_phone():
    web = Web().login()
    rkey = web.send(RED)
    assert web.task()["step"] == "confirm"
    web.gesture()
    task = web.task()
    assert task["step"] == "mfa_request"
    assert task["gesture"]["label"] == "获取短信验证码（模拟）"
    assert task["mfa"]["method_text"] == tr("polish.mfa.other_methods")
    web.gesture()
    task = web.task()
    assert task["step"] == "mfa_code" and task["gesture"]["op"] == "mfa_verify"
    code = web.code(task["key"])
    assert code not in dumps(web.state())
    with catalog.bound(web.ws):
        phone = vm.payload(web.ws.pair, allowed=True)
    assert phone["messages"][0]["latest"] and code in phone["messages"][0]["text"]
    web.gesture()
    task = web.task()
    assert task["step"] == "final_confirm" and task["gesture"]["label"] == "再次确认"
    web.gesture()
    assert web.task(rkey)["step"] == "done"
    with catalog.bound(web.ws):
        phone = vm.payload(web.ws.pair, allowed=True)
    assert phone["messages"][0]["status"] == "used" and code not in dumps(phone)


def test_a_new_payee_waits_for_both_answers_before_the_device_offers_a_gesture():
    web = Web().login()
    rkey = web.send(NEW)
    response = web.response(rkey)
    gate = response["gate"]
    assert gate["decision"] == "incomplete" and gate["interactive"] and not gate["locked"]
    assert [question["name"] for question in gate["questions"]] == ["verified", "guided"]
    assert gate["notice"] == tr("polish.scam.incomplete")
    assert gate["footer"] == scam_friction.footer()
    assert "judge" not in gate
    task = web.task(rkey)
    assert task["step"] == "gate_blocked" and task["gesture"] is None
    assert task["gate_notice"]["text"] == tr("polish.scam.device_incomplete")
    assert task["back"]["rkey"] == rkey
    web.answer(SAFE)
    assert web.response(rkey)["gate"]["decision"] == "proceed"
    assert web.task(rkey)["gesture"]["op"] == "confirm"


def test_words_that_someone_told_the_user_to_pay_preset_and_latch_a_pause():
    web = Web(view="judge").login()
    rkey = web.send(DIRECTED_NEW)
    gate = web.response(rkey)["gate"]
    assert gate["decision"] == "pause" and gate["preset"] and gate["locked"]
    assert gate["questions"][1]["answer"] == "yes"
    assert gate["pause"]["copy"].startswith(tr("polish.scam.pause_lead_preset"))
    assert gate["judge"]["preset_note"] == tr("polish.scam.preset_judge")
    assert web.task(rkey)["step"] == "gate_blocked"
    web.retry()
    gate = web.response(rkey)["gate"]
    assert gate["decision"] == "incomplete" and not gate["preset"]
    assert gate["judge"]["preset_note"] is None


def test_an_authority_cue_opens_the_reply_with_the_anti_fraud_line():
    web = Web().login()
    web.send(OPENING)
    assert web.response()["copy_lines"][0] == scam_friction.opening(("authority",))


@pytest.mark.parametrize("held", ["text", "drill"])
def test_the_phrase_gate_shows_its_fixed_words_with_no_card_and_no_task(held):
    web = Web(view="judge").login()
    rkey = web.send(HELD) if held == "text" else web.shortcut("scam_drill")
    page = web.state()
    response = page["chat"]["responses"][rkey]
    assert response["cards"] == [] and response["gate"] is None and response["cancel"] is None
    assert "96110" in "".join(response["copy_lines"])
    assert response["phrase_gate_note"] == tr("polish.scam.phrase_gate_judge")
    assert page["device"]["tasks"] == [] and page["evidence"]["dispatch"]["count"] == 0


def test_the_recipient_choice_locks_once_the_draft_is_in_use():
    web = Web().login()
    rkey = web.send(CLARIFY_PLAIN)
    clarify = web.response(rkey)["clarify"]
    assert [choice["key"] for choice in clarify["choices"]] == ["c0", "c1"]
    assert clarify["chosen"] is None and not clarify["locked"]
    assert "evidence_lines" not in clarify
    web.post("select_recipient", rkey, "c1")
    response = web.response(rkey)
    assert response["clarify"]["chosen"] == "c1" and len(response["cards"]) == 1
    assert without_key(response["cards"]) == without_key(web.task(rkey)["cards"])
    web.gesture(rkey)
    clarify = web.response(rkey)["clarify"]
    assert clarify["locked"] and clarify["locked_text"] == catalog.wtr("web.vm.clarify.locked")


def test_only_the_current_reply_holds_a_token():
    web = Web().login()
    first = web.send(YELLOW)
    second = web.send(RED)
    page = web.state()
    assert page["chat"]["current"] == second
    assert page["device"]["active"] == "T" + second
    tasks = {task["rkey"]: task for task in page["device"]["tasks"]}
    assert tasks[second]["gesture"] is not None and tasks[second]["current"]
    old = tasks[first]
    assert old["step"] == "not_current" and old["gesture"] is None and not old["current"]
    assert old["back"] == {"rkey": first, "label": catalog.wtr("web.vm.device.back")}
    assert page["device"]["queue_text"] == catalog.wtr("web.vm.device.queue", count=1)


def test_three_wrong_codes_lock_the_shell_and_close_the_device_gestures():
    web = Web(view="judge").login()
    task = web.red_until_code()
    wrong = f"{(int(web.code(task['key'])) + 1) % 1_000_000:06d}"
    for _ in range(3):
        web.gesture(code=wrong)
    page = web.state()
    safety = page["shell"]["safety"]
    assert safety["locked"] and safety["state"] == "Locked" and safety["reason"]
    assert safety["text"] == tr(
        "Locked: yellow and red actions are blocked. Green queries remain available."
    )
    assert safety["hint"] == catalog.wtr("web.vm.safety.way_out")
    task = page["device"]["tasks"][0]
    assert task["step"] == "account_locked" and task["gesture"] is None
    assert task["cancel"] is not None
    web.switch(view="customer")
    safety = web.state()["shell"]["safety"]
    assert safety["locked"] and set(safety) == {"locked", "text", "hint"}


def test_judge_evidence_opens_three_sections_and_states_what_the_hashes_are():
    web = Web(view="judge").login()
    web.send(RED)
    page = web.state()
    evidence = page["evidence"]
    assert evidence["open_by_default"] == ["kernel", "consistency", "dispatch"]
    draft = evidence["consistency"]
    assert draft["proposal_card"] is None and draft["same_hash"] is None
    assert draft["not_yet_text"] == catalog.wtr("web.vm.evidence.not_yet")
    assert page["device"]["tasks"][0]["hash"]["status"] == "not_yet"
    web.gesture()
    page = web.state()
    consistency = page["evidence"]["consistency"]
    display = consistency["display_card"]
    assert display["algorithm"] == "SHA-256(JCS)" and display["short"] == display["full"][:16]
    assert consistency["same_hash"] is True and consistency["same_business_facts"] is True
    assert consistency["not_yet_text"] is None and consistency["challenge"] == "none"
    assert page["evidence"]["kernel"]["outcome"] == "Escalate"
    task_hash = page["device"]["tasks"][0]["hash"]
    assert task_hash["status"] == "pending" and task_hash["full"] == display["full"]
    assert page["evidence"]["audit"]["note"] == catalog.wtr("web.vm.evidence.audit_note")


def test_a_language_switch_keeps_the_card_identity_the_gate_and_the_token():
    web = Web().login()
    rkey = web.send(FRAUD)
    web.answer(SAFE)
    before = web.state()
    web.switch(locale="en")
    after = web.state()
    old, new = before["chat"]["responses"][rkey], after["chat"]["responses"][rkey]
    assert [fact["key"] for fact in old["cards"][0]["facts"]] == [
        fact["key"] for fact in new["cards"][0]["facts"]
    ]
    assert new["cards"][0]["amount_text"] == "CNY 480.00"
    assert old["gate"]["version"] == new["gate"]["version"]
    held, kept = before["device"]["tasks"][0]["gesture"], after["device"]["tasks"][0]["gesture"]
    assert (held["token"], held["card_version"]) == (kept["token"], kept["card_version"])
    assert kept["label"] == "Confirm"
    assert old["gate"]["questions"][0]["answer"] == new["gate"]["questions"][0]["answer"] == "yes"


def test_the_session_view_decides_even_when_the_caller_forgot_to_bind_it():
    web = Web().login()
    web.send(RED)
    token = set_audience("judge")
    try:
        page = vm.app_state(web.ws)
    finally:
        reset_audience(token)
    assert "evidence" not in page and "hash" not in page["device"]["tasks"][0]
    assert "technical" not in page["chat"]["responses"]["R1"]["cards"][0]


def test_reading_the_page_changes_nothing():
    web = Web(view="judge").login()
    rkey = web.send(FRAUD)
    web.answer(SAFE)
    web.gesture()
    before = (snapshot(web.controller, web.ws.session), dict(web.ws.gestures), dict(web.ws.gates))
    held = H(web.controller.state)
    for _ in range(3):
        vm.app_state(web.ws)
        vm.tab(web.ws, "operator")
        vm.i18n_strings(web.ws)
        vm.technical(web.ws, rkey)
    after = (snapshot(web.controller, web.ws.session), dict(web.ws.gestures), dict(web.ws.gates))
    assert before == after and H(web.controller.state) == held


def test_turns_show_the_words_as_sent_and_the_marker_turns():
    web = Web().login()
    web.send(CLARIFY_PLAIN)
    web.post("chat_stop")
    web.post("stop_all")
    turns = web.state()["chat"]["turns"]
    assert [turn["role"] for turn in turns] == [
        "user",
        "assistant",
        "user",
        "assistant",
        "system",
    ]
    assert "NAME_" not in turns[0]["text"] and "（合成）" in turns[0]["text"]
    assert turns[2]["text"] == catalog.wtr("web.vm.turn.cancel")
    assert turns[4]["text"].startswith(catalog.wtr("web.vm.turn.stop_all"))
    judge = Web(view="judge").login()
    judge.send(CLARIFY_PLAIN)
    assert judge.state()["chat"]["turns"][0]["text"] == judge.ws.transcript[0].hint


def test_the_phone_payload_reads_only_its_own_pairing():
    web = Web().login()
    with catalog.bound(web.ws):
        unpaired = vm.payload(web.ws.pair, allowed=False)
        empty = vm.payload(web.ws.pair, allowed=True)
    assert unpaired["messages"] == [] and unpaired["unpaired"] == tr("polish.phone.unpaired")
    assert empty["empty"] == tr("polish.phone.empty") and empty["unpaired"] is None
    assert "disclosure" not in empty and re.fullmatch(r"[0-9a-f]{16}", empty["version"])
    contracts.validate_response(empty, view="customer")
    web.switch(view="judge")
    with catalog.bound(web.ws):
        judged = vm.payload(web.ws.pair, allowed=True)
    assert judged["disclosure"] == tr("polish.phone.disclosure")


def test_shell_strings_keep_their_placeholders_and_the_customer_overrides():
    web = Web()
    strings = vm.i18n_strings(web.ws)
    assert "{count}" in strings["web.ui.device.queue"]
    assert (
        strings["web.ui.device.title"]
        == catalog.WEB_MESSAGES["customer:web.ui.device.title"]["zh-CN"]
    )
    assert all(key.startswith("web.ui.") or key in vm.app.REUSED for key in strings)


def test_the_operator_tab_shows_the_redacted_handoff_snapshot(core_only):
    web = Web(view="judge").login()
    web.send(YELLOW)
    web.post("handoff")
    operator = vm.tab(web.ws, "operator")
    assert operator["snapshot_lines"][0] == tr("Latest conversation handoff snapshot")
    assert operator["snapshot"]["intent"] == "handoff"
    assert {"response_id", "intent", "status"} <= set(operator["snapshot"]["session_snapshot"][0])
    web.switch(view="customer")
    operator = vm.tab(web.ws, "operator")
    assert "snapshot" not in operator and operator["compensation"] is None


# --- ext items (RW-3 order): birthday and refund, subscriptions, bills, wealth ------------

EXT = frozenset({
    "tab:accounts",
    "tab:plans",
    "tab:wealth",
    "shortcut:subscriptions",
    "shortcut:billing",
    "shortcut:birthday",
    "plan_select",
    "compensate",
})


@pytest.fixture
def ext(monkeypatch):
    import corridor.web as web_package

    monkeypatch.setattr(web_package, "FEATURES", web_package.FEATURES | EXT)


def tab(web, name):
    shown = vm.tab(web.ws, name)
    body = {
        "ok": True,
        "generation": web.ws.generation,
        "revision": web.ws.revision,
        "tab": {"name": name, "revision": web.ws.revision, "vm": shown},
    }
    contracts.validate_response(body, view=web.ws.view)
    return shown


def business(web, source):
    return [task for task in web.state()["device"]["tasks"] if task["source"] == source]


def confirm(web, task):
    held = task["gesture"]
    return web.post(OPS[held["op"]], task["key"], held["token"], held["card_version"])


def test_a_birthday_without_a_relation_lists_the_calendar_choices(ext):
    web = Web().login()
    rkey = web.send("帮我准备生日")
    birthday = web.response(rkey)["birthday"]
    assert [choice["key"] for choice in birthday["choices"]] == ["c0", "c1"]
    assert all(choice["enabled"] for choice in birthday["choices"])
    web.post("choose_birthday", rkey, "c1")
    (task,) = business(web, "plans")
    assert task["step"] == "select_nodes" and task["nodes_version"]
    draft = tab(web, "plans")["draft"]
    assert draft["task"] == task["key"] and draft["select"]["total"] == 4


def test_a_confirmed_birthday_plan_then_a_refund_card_goes_through_the_device(ext):
    web = Web().login()
    rkey = web.send("帮我准备老婆的生日")
    task = web.task(rkey)
    web.post("select_nodes", task["key"], task["nodes_version"], ["n0", "n1", "n2", "n3"])
    task = web.task(rkey)
    assert task["step"] == "confirm_plan" and len(task["cards"]) == 4
    assert task["selection"]["text"] == catalog.wtr("web.vm.plan.selection_all", total=4)
    web.gesture(rkey)
    plans = tab(web, "plans")
    statuses = [node["status"] for node in plans["plans"][0]["dag"]["nodes"]]
    assert statuses[0] == "succeeded" and plans["plans"][0]["blocked_text"] is None
    web.send("停止")
    assert tab(web, "plans")["plans"][0]["blocked_text"]
    refund = web.send("退回锁定的预算")
    response = web.response(refund)
    assert [card["purpose"] for card in response["cards"]] == ["解锁预算"]
    assert web.task(refund)["step"] == "confirm_intent"
    web.gesture(refund)
    assert web.task(refund)["step"] == "done"


def test_the_task_panel_offers_compensate_only_when_open(ext):
    web = Web().login()
    web.send(YELLOW)
    assert web.state()["chat"]["task_panel"]["controls"]["compensate"] == tr("Compensate")
    version = web.state()["chat"]["task_panel"]["controls"]["compensate_version"]
    web.post("compensate_latest", version)
    operator = tab(web, "operator")
    assert operator["compensation"]["result"]["banner"]["tone"] == "info"


def test_a_subscription_query_on_the_accounts_tab_settles_and_offers_cancellation(ext):
    web = Web().login()
    web.post("subscriptions_query")
    (task,) = business(web, "subscriptions")
    assert task["step"] == "confirm_intent" and task["current"]
    assert tab(web, "accounts")["sub_query"]["task"] == task["key"]
    confirm(web, task)
    accounts = tab(web, "accounts")
    rows = accounts["subscriptions"]["rows"]
    choices = accounts["subscriptions"]["cancel_choices"]
    assert rows and choices and choices[0]["key"] == "c0"
    web.post("subscriptions_cancel", "c0", accounts["subscriptions"]["cancel_version"])
    pending = [task for task in business(web, "subscriptions") if task["step"] != "done"]
    assert pending and pending[0]["cards"][0]["purpose"] == tr("Cancel subscription")


def test_the_accounts_tab_lists_balances_cards_receipts_and_the_latest_bill(ext):
    web = Web().login()
    web.send(YELLOW)
    web.gesture()
    web.send("看看9月账单")
    accounts = tab(web, "accounts")
    assert {row["label"] for row in accounts["balances"]} >= {"借记卡尾号6011（合成）"}
    assert accounts["receipts"][0]["amount"] == "¥500.00"
    assert accounts["billing"]["totals"] and accounts["empty_text"] is None


def test_the_wealth_tab_compares_previews_refuses_in_isolation_and_redeems(ext):
    web = Web().login()
    web.post("wealth_compare", "net_return")
    wealth = tab(web, "wealth")
    comparison = wealth["comparison"]
    assert [row["key"] for row in comparison["rows"]] == ["c0", "c1"]
    assert comparison["rows"][0]["chosen"] and comparison["exclusions"]
    assert [item["key"] for item in comparison["candidates"]] == ["c0", "c1"]
    assert wealth["scope_note"] == catalog.wtr("web.vm.route.wealth_note")
    web.post("wealth_purchase", None, comparison["candidates_version"])
    purchase = tab(web, "wealth")["purchase"]
    assert purchase["card"]["notes"][0]["kind"] == "suitability" and "refusal" not in purchase
    (task,) = business(web, "wealth")
    assert purchase["task"] == task["key"]
    assert without_key(task["cards"]) == without_key([purchase["card"]])
    web.post("preset_r4")
    r4 = tab(web, "wealth")["r4"]
    assert r4["title"] == "无法申购：产品风险等级高于您的风险承受能力"
    assert r4["reasons"] and "rule_ids" not in r4
    assert [task["key"] for task in business(web, "wealth")] == [task["key"]]
    positions = tab(web, "wealth")["positions"]
    assert positions and positions[0]["key"] == "c0" and positions[0]["redeemable"]
    web.post("wealth_redeem", "c0", tab(web, "wealth")["positions_version"])
    assert tab(web, "wealth")["redemption"]["task"]
