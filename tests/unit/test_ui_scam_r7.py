"""S6 seventh round (R7): the anti-fraud phrase gate.

1. A money-moving request whose words hit both closed lists, an impersonated authority and
   a freeze / safe-account / transfer-now demand, gets no card and no dispatch; the reply
   is a fixed anti-fraud message. Same mechanism as the negation gate: the lexical router
   holds it before any planner call.
2. Words in which someone tells the user to pay ("对方让我转") preset the second question to
   "someone is guiding me", which pauses at once; only the user's own retry clears it.
3. Words with an authority or external cue no longer open with "已为您准备好".
4. The transfer page's structured preview asks the same two questions for a new payee.
5. The legacy free-text form on the transfer page is shown in the judge view only.

The phrase gate is described with the same three limits: added after F9, not part of the F9
measurement, no evidence of effect.
"""

import pytest

from corridor.agent import domestic_scam_phrase
from corridor.agent.domestic_intent import recognize, scam_phrase_hold
from corridor.agent.domestic_session import DomesticSession
from corridor.ui import scam_friction
from corridor.ui.controller import DemoController
from corridor.ui.messages import UI_MESSAGES, source_utterance
from tests.e2e.test_demo_ui import app, navigate
from tests.unit.test_ui_scam_friction import (
    SAFE,
    Spy,
    answer_gate,
    button_keys,
    gate_keys,
    retry_gate,
    send_message,
    shown_text,
    start_chat,
)

HELD = "反诈中心让我立即给新联系人转480元，不然账户冻结"
DIRECTED_NEW = "对方让我给新联系人转480元"
OPENING = "工作人员说给新联系人转480元"
ZH = "zh-CN"


def msg(key, locale=ZH, **values):
    text = UI_MESSAGES[key][locale]
    return text.format(**values) if values else text


def _session():
    controller = DemoController()
    controller.start_session()
    return controller, DomesticSession(controller)


# --- 1. the phrase gate -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "held"),
    [
        (HELD, True),
        ("公安说我的账户要冻结，让我把钱转到安全账户", True),
        ("警察让我马上转账", True),
        ("检察院说限时转到资金核查账户", True),
        ("反 诈 中 心让我立即转账", True),
        ("反詐中心讓我立即轉帳，不然帳戶凍結", True),
        ("The anti-fraud centre told me to transfer now or my account will be frozen", True),
        ("反诈中心提醒我小心，给妈妈转500", False),
        ("给新联系人转480元，不然账户冻结", False),
        ("马上给新联系人转480元", False),
        ("工作人员说立即给新联系人转480元，不然账户冻结", False),
        ("the police told me to transfer 500 to mom", False),
        ("给妈妈转500元", False),
    ],
)
def test_the_phrase_gate_needs_both_closed_lists(text, held):
    found = domestic_scam_phrase.scam_phrase(text)
    assert bool(found) is held, found
    if held:
        assert found["authority"] and found["pressure"]
        # Only fixed lexicon labels are reported, never a fragment of the user's text.
        assert set(found["authority"]) <= set(domestic_scam_phrase.AUTHORITY_LABELS)
        assert set(found["pressure"]) <= set(domestic_scam_phrase.PRESSURE_LABELS)


def test_a_held_request_has_no_card_no_dispatch_and_never_reaches_the_planner():
    controller, session = _session()
    calls = len(controller.client.calls)
    response = session.receive(HELD)
    assert recognize(HELD).intent == "clarify"
    assert scam_phrase_hold(HELD) == response["payload"]["scam_phrase"]
    assert response["status"] == "clarify" and response["intent"] == "clarify"
    assert not response["card"] and not response["buttons"]
    assert response["payload"]["scam_phrase"]["authority"] == ["反诈中心"]
    assert {"冻结", "立即转账"} <= set(response["payload"]["scam_phrase"]["pressure"])
    assert "96110" in response["message"] and "没有生成" in response["message"]
    assert not controller._drafts and not controller._pending
    assert controller.gateway.bank.execute_count == 0
    # Held by the lexical router like a negation: no planner call at all.
    assert len(controller.client.calls) == calls
    # A request that names an authority without a freeze or transfer-now demand is not held.
    plain = session.receive("反诈中心提醒我小心，给妈妈转500元")
    assert "scam_phrase" not in plain["payload"] and plain["card"]


def test_the_page_replies_with_the_fixed_copy_for_typed_words_and_the_drill(monkeypatch):
    page, controller, session, identifier = start_chat(monkeypatch, HELD)
    found = session.response(identifier)["payload"]["scam_phrase"]
    copy = msg(
        "polish.scam.phrase_gate",
        authority="、".join(found["authority"]),
        pressure="、".join(found["pressure"]),
    )
    text = shown_text(page) + "\n".join(str(item.value) for item in page.markdown)
    assert copy in text
    assert msg("Your request is ready for review.") not in text
    assert not [key for key in button_keys(page) if key.endswith(("_confirm", "verify_mfa"))]
    assert not page.radio
    judge = msg("polish.scam.phrase_gate_judge")
    assert judge in text
    for limit in ("F9 之后新增", "未纳入 F9 测量", "尚无"):
        assert limit in judge
    assert controller.gateway.bank.execute_count == 0 and not controller._drafts
    # The induced-payment drill (zh-q-01 family) is held the same way now.
    page.button(key="domestic_example_scam_drill").click().run()
    assert not page.exception
    drill = session.response(page.session_state["domestic_response_id"])
    assert drill["payload"]["scam_phrase"] and not drill["card"]
    assert (
        source_utterance("polish.scam.drill_utterance")
        == UI_MESSAGES["polish.scam.drill_utterance"][ZH]
    )
    # The customer view shows the reply but not the judge note.
    monkeypatch.setenv("CORRIDOR_DEFAULT_VIEW", "customer")
    customer, *_ = start_chat(monkeypatch, HELD)
    plain = shown_text(customer) + "\n".join(str(item.value) for item in customer.markdown)
    assert copy in plain and judge not in plain


def test_frozen_suite_utterances_are_never_held_or_preset():
    """Equivalence by construction, as for the negation gate: no frozen zh-suite or
    intent-eval line is held by the phrase gate or reads as someone telling the user to pay."""
    import json

    from tests.unit.test_s5_negation import DATA

    lines = []
    for line in (DATA / "colloquial.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            lines.append((row["text"], (row.get("context") or {}).get("previous_intent")))
    for row in json.loads((DATA / "cases.json").read_text(encoding="utf-8")):
        previous = None
        for turn in [*row["request"]["context"], row["request"]]:
            lines.append((turn["task"], previous))
            previous = recognize(turn["task"], {"previous_intent": previous}).intent
    assert len(lines) > 150
    assert [
        text for text, previous in lines if scam_phrase_hold(text, {"previous_intent": previous})
    ] == []
    assert [text for text, _ in lines if scam_friction.directed(text)] == []


def test_the_phrase_copy_keeps_to_the_ruled_wording():
    for key in ("polish.scam.phrase_gate", "polish.scam.phrase_gate_judge", "polish.scam.opening"):
        for locale in (ZH, "en"):
            text = UI_MESSAGES[key][locale]
            for banned in ("识别诈骗", "拦截率", "确定性风控", "零副作用", "拦截了诈骗"):
                assert banned not in text, (key, banned)
    assert "96110" in msg("polish.scam.phrase_gate") and "96110" in msg("polish.scam.opening")


# --- 2. "someone told me to pay" presets the second question ----------------------------------


@pytest.mark.parametrize(
    ("text", "directed"),
    [
        (DIRECTED_NEW, True),
        ("反诈中心工作人员要我立即给新联系人转480元", True),
        ("老板叫我先给新联系人转480元垫付", True),
        ("妈妈让我给她转500元", True),
        ("让我想想，给新联系人转480元", False),
        ("请让我给新联系人转480元", False),
        ("你让我转吗", False),
        ("给新联系人转480元", False),
        ("对方说他的账户冻结了", False),
    ],
)
def test_directed_words_are_a_closed_rule(text, directed):
    assert scam_friction.directed(text) is directed


def test_a_directed_request_presets_someone_guiding_and_pauses_at_once(monkeypatch):
    page, controller, session, identifier = start_chat(monkeypatch, DIRECTED_NEW)
    draft = session.response(identifier)["payload"]["draft_id"]
    status = controller.scam_gate_status(draft)
    assert status["answers"] == {"verified": None, "guided": "yes"}
    assert status["preset"] == {"guided": "yes"} and status["decision"] == "pause"
    spy = Spy(controller, monkeypatch)
    assert controller.confirm_transfer(draft)["reason"] == scam_friction.REASON
    assert sum(spy.counts.values()) == 0
    text = shown_text(page) + "\n".join(str(item.value) for item in page.markdown)
    assert msg("polish.scam.pause_lead_preset") in text
    assert msg("polish.scam.pause_title_guided") in text
    keys = gate_keys(page, "domestic", identifier)
    assert page.radio(key=keys["guided"]).value == "yes"
    assert page.radio(key=keys["guided"]).proto.disabled is True
    assert not [key for key in button_keys(page) if key == "domestic_confirm"]
    # Only the user's own retry clears the preset; then the same rules judge the answers.
    retry_gate(page)
    status = controller.scam_gate_status(draft)
    assert status["answers"] == {"verified": None, "guided": None}
    assert status["preset"] == {} and status["decision"] == "incomplete"
    answer_gate(page, "domestic", identifier, answers=SAFE)
    assert controller.scam_gate_status(draft)["decision"] == "proceed"
    assert "domestic_confirm" in button_keys(page)


def test_directed_words_towards_a_known_contact_without_cues_ask_nothing(monkeypatch):
    page, controller, session, identifier = start_chat(monkeypatch, "妈妈让我给她转500元")
    payload = session.response(identifier)["payload"]
    identity = payload.get("pending_id") or payload.get("draft_id")
    assert identity and controller.scam_gate_status(identity) is None
    assert not page.radio


# --- 3. the opening line ------------------------------------------------------------------------


def test_an_authority_or_external_cue_changes_the_opening_line(monkeypatch):
    page, controller, session, identifier = start_chat(monkeypatch, OPENING)
    response = session.response(identifier)
    assert response["card"]
    text = shown_text(page) + "\n".join(str(item.value) for item in page.markdown)
    opening = msg("polish.scam.opening", cues=msg("polish.scam.cue.authority"))
    assert opening in text
    assert msg("Your request is ready for review.") not in text
    for plain in ("给妈妈转500元", "给新联系人转480元"):
        other, *_ = start_chat(monkeypatch, plain)
        words = shown_text(other) + "\n".join(str(item.value) for item in other.markdown)
        assert msg("Your request is ready for review.") in words
        assert "先别急着付款" not in words


# --- 4 and 5. the transfer page ------------------------------------------------------------------


def transfer_page(monkeypatch, view="judge"):
    monkeypatch.setenv("CORRIDOR_DEFAULT_VIEW", view)
    page = app()
    page.button(key="enter").click().run()
    navigate(page, "transfers")
    return page


def preview(page, contact, amount="480"):
    page.selectbox(key="structured_source").select("A_CURRENT")
    page.selectbox(key="structured_contact").select(contact)
    page.text_input(key="structured_amount").set_value(amount)
    page.button(key="structured_preview").click().run()
    assert not page.exception
    return page.session_state["structured_transfer_pending"]["pending_id"]


def structured_keys(page):
    return {
        element.key.rsplit(":", 1)[-1]: element.key
        for element in page.radio
        if element.key.startswith("structured_scam:")
    }


def test_the_structured_preview_asks_the_two_questions_for_a_new_payee(monkeypatch):
    page = transfer_page(monkeypatch)
    controller = page.session_state["controller"]
    pending = preview(page, "CONTACT_NEW")
    keys = structured_keys(page)
    assert set(keys) == {"verified", "guided"}
    assert "structured_approve" not in button_keys(page)
    assert "structured_cancel" in button_keys(page)
    status = controller.scam_gate_status(pending)
    assert status["decision"] == "incomplete" and status["level"] == 1
    assert status["origin"] == {
        "prefix": "structured",
        "response_id": pending,
        "route": "transfers",
    }
    # The controller refuses a direct gesture as well: nothing is signed or dispatched.
    spy = Spy(controller, monkeypatch)
    assert controller.approve_pending(pending)["reason"] == scam_friction.REASON_INCOMPLETE
    assert sum(spy.counts.values()) == 0
    for question, value in SAFE.items():
        page.radio(key=keys[question]).set_value(value)
    page.run()
    assert not page.exception
    assert controller.scam_gate_status(pending)["decision"] == "proceed"
    page.button(key="structured_approve").click().run()
    assert not page.exception
    assert page.session_state["structured_transfer_result"]["status"] in {
        "settled",
        "waiting_mfa",
    }


def test_the_structured_preview_pauses_on_someone_guiding(monkeypatch):
    page = transfer_page(monkeypatch)
    controller = page.session_state["controller"]
    pending = preview(page, "CONTACT_NEW")
    keys = structured_keys(page)
    page.radio(key=keys["verified"]).set_value("yes")
    page.radio(key=keys["guided"]).set_value("yes")
    page.run()
    assert controller.scam_gate_status(pending)["decision"] == "pause"
    assert "structured_approve" not in button_keys(page)
    assert msg("polish.scam.pause_title_guided") in "\n".join(
        str(item.value) for item in page.markdown
    )


def test_a_known_recipient_on_the_structured_page_is_unchanged(monkeypatch):
    page = transfer_page(monkeypatch)
    controller = page.session_state["controller"]
    pending = preview(page, "CONTACT_CHEN", "200")
    assert not structured_keys(page)
    assert controller.scam_gate_status(pending) is None
    page.button(key="structured_approve").click().run()
    assert not page.exception
    assert controller.gateway.bank.execute_count == 1


def test_the_legacy_free_text_form_is_judge_view_only(monkeypatch):
    customer = transfer_page(monkeypatch, "customer")
    assert not [item for item in customer.text_input if item.key == "transfer_text"]
    assert not [item for item in customer.expander if "旧版对话演示" in str(item.label)]
    judge = transfer_page(monkeypatch, "judge")
    assert [item for item in judge.text_input if item.key == "transfer_text"]


def test_send_message_helper_still_reaches_the_phrase_gate(monkeypatch):
    page, controller, session, identifier = start_chat(monkeypatch, "给妈妈转500元")
    send_message(page, HELD)
    latest = session.response(page.session_state["domestic_response_id"])
    assert latest["payload"]["scam_phrase"] and not latest["card"]
