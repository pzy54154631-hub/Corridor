"""C2: the review of S6 round 7 (phrase gate and the recipient check's coverage).

1. The phrase gate is decided before every branch that can move money (birthday plans,
   purchases), with more authority names and English words matched as whole words.
2. The recipient check fails closed at the controller: a payment that needs the two
   questions (a new payee, or authority / external cues in the user's words) is refused at
   every gesture entry until they are answered, whether or not a page ever drew them.
3. No product-layer text says the gate "has no side effect"; the preset is described as
   a keyword rule that may be wrong; "让我转告" is not someone telling the user to pay.

No test calls a model.
"""

import json
import re
from pathlib import Path

import pytest

from corridor.agent import domestic_scam_phrase
from corridor.agent.domestic_scam_phrase import scam_phrase
from corridor.agent.domestic_session import DomesticSession
from corridor.channels.im import IMAdapter
from corridor.crypto.jcs import H
from corridor.ui import fraud_hint, scam_friction
from corridor.ui.controller import DemoController, demo_state
from corridor.ui.messages import UI_MESSAGES

SRC = Path(domestic_scam_phrase.__file__).resolve().parents[1]


def _session(*, showcase=False):
    controller = DemoController(initial_state=demo_state(showcase=showcase))
    controller.start_session()
    return DomesticSession(controller)


class _Spy:
    """Counts planner calls without changing them."""

    def __init__(self, client):
        self.client, self.calls = client, 0
        self.mode, self.model = client.mode, client.model

    def complete(self, **kwargs):
        self.calls += 1
        return self.client.complete(**kwargs)


def _quiet(session):
    controller = session.controller
    return (
        controller.gateway.bank.execute_count,
        len(controller.state.user_inputs),
        H(controller.state.schedules),
        H(controller.state.positions),
        len(controller._drafts) + len(controller._pending),
    )


# --- 1. The phrase gate before every money-moving branch --------------------------------


@pytest.mark.parametrize(
    ("showcase", "text"),
    [
        (True, "生日，公安说明天冻结账户，赶紧给妈妈打钱500"),
        (True, "爱人生日，警察说账户要冻结，马上给她转1000块"),
        (False, "公安让我马上把钱买入安全账户理财"),
        (False, "反诈中心说要冻结，赶紧申购理财"),
        (False, "公检法的人说我涉嫌洗钱，账户要冻结，让我马上给新联系人转480元"),
        (False, "最高检说我的账户要冻结，让我马上给新联系人转480元"),
        (False, "国安局说我的账户要冻结，让我马上给新联系人转480元"),
        (False, "银监会说我的账户要冻结，让我马上给新联系人转480元"),
        (False, "银行客服说我的账户要冻结，让我马上给新联系人转480元"),
    ],
)
def test_the_phrase_gate_holds_before_any_money_moving_branch(showcase, text):
    session = _session(showcase=showcase)
    spy = _Spy(session.client)
    session.client = spy
    before = _quiet(session)
    response = session.receive(text)
    assert response["intent"] == "clarify", (text, response["intent"])
    assert response["payload"]["scam_phrase"]["authority"], text
    assert not response["card"] and not response["payload"].get("nodes"), text
    assert "purchase" not in response["payload"] and response["buttons"] == [], text
    # Decided lexically, before any planner call; nothing prepared or signed.
    assert spy.calls == 0, text
    assert _quiet(session) == before, text


@pytest.mark.parametrize(
    "text",
    [
        "Please transfer 50 to mom right away, a courtesy refund",
        "Transfer 500 to Courtney right now",
        "Transfer 500 to mom now, the freezer is broken",
    ],
)
def test_english_entries_match_whole_words_only(text):
    assert scam_phrase(text) is None, text


# --- 2. The recipient check fails closed at the controller ------------------------------


def _new_payee_card(session):
    response = session.receive("给新联系人转480元")
    assert response["card"]["kind"] == "transfer"
    assert "new_recipient" in response["card"]["tier_reasons"]
    return response


def test_a_new_payee_payment_confirmed_without_the_page_is_refused(monkeypatch):
    session = _session()
    controller = session.controller
    requested = []
    original = controller.gateway.request_mfa
    monkeypatch.setattr(
        controller.gateway,
        "request_mfa",
        lambda *args, **kwargs: requested.append(args) or original(*args, **kwargs),
    )
    response = _new_payee_card(session)
    # No page drew the questions; the device gesture arrives straight from the session.
    result = session.confirm(response["response_id"])
    assert result["status"] == scam_friction.STATUS, result["status"]
    assert result["payload"]["reason"] == scam_friction.REASON_INCOMPLETE
    assert "两道自查" in result["message"] or "两个自查" in result["message"]
    pending = result["payload"].get("pending_id") or result["payload"].get("draft_id")
    assert pending
    for gesture in (controller.request_mfa, controller.complete_mfa, controller.approve_pending):
        if pending in controller._pending:
            assert gesture(pending)["status"] == scam_friction.STATUS
    assert requested == [] and controller.gateway.bank.execute_count == 0
    # Answering the two questions (the page's own path) lets the payment continue.
    status = controller.scam_gate_status(pending)
    controller.scam_gate_answer(
        pending,
        card_identity=status["card_identity"],
        epoch=status["epoch"],
        answers={"verified": "yes", "guided": "no"},
    )
    assert controller.scam_gate_status(pending)["decision"] == "proceed"
    assert session.confirm(response["response_id"])["status"] == "waiting_mfa"


def test_the_im_text_channel_meets_the_same_refusal():
    session = _session()
    adapter = IMAdapter(session)
    response = adapter.receive("给新联系人转480元")
    assert response["buttons"]
    result = adapter.click("confirm", response["response_id"])
    assert result["status"] == scam_friction.STATUS
    assert "自查" in result["text"]
    assert session.controller.gateway.bank.execute_count == 0


def test_a_scheduled_transfer_page_payment_to_a_new_payee_is_refused_until_answered():
    session = _session()
    controller = session.controller
    pending = controller.preview_scheduled_transfer(
        "A_CURRENT", "CONTACT_NEW", "480", "page-schedule", date_text="2026-11-20"
    )
    assert controller.approve_pending(pending["pending_id"])["status"] == scam_friction.STATUS
    assert controller.gateway.bank.execute_count == 0


def test_words_with_an_authority_cue_gate_a_known_contact_too():
    session = _session()
    response = session.receive("工作人员说给妈妈转500元，收款账号是对方发来的")
    assert response["card"]["kind"] == "transfer"
    result = session.confirm(response["response_id"])
    assert result["status"] == scam_friction.STATUS


def test_a_known_contact_without_cues_is_unchanged():
    session = _session()
    response = session.receive("给妈妈转500元")
    assert session.confirm(response["response_id"])["status"] == "settled"


# --- 3. Wording ---------------------------------------------------------------------------

BANNED = ("没有副作用", "无副作用", "零副作用", "no side effect", "side-effect free")


def test_no_product_layer_text_claims_the_gate_has_no_side_effect():
    for key in ("polish.scam.phrase_gate_judge",):
        for locale, text in UI_MESSAGES[key].items():
            assert not [word for word in BANNED if word in text.lower()], (key, locale)
    found = []
    for folder in ("agent", "ui", "channels"):
        for path in (SRC / folder).rglob("*"):
            if path.suffix in {".py", ".json", ".html", ".js"} and path.is_file():
                text = path.read_text(encoding="utf-8").lower()
                found.extend((path.name, word) for word in BANNED if word in text)
    assert found == []


def test_the_judge_note_states_the_limits_in_the_fixed_shape():
    zh = UI_MESSAGES["polish.scam.phrase_gate_judge"]["zh-CN"]
    for part in ("不建草稿", "不出卡", "派发为 0", "不修改冻结内核", "不覆盖", "不判断", "F9"):
        assert part in zh, part
    en = UI_MESSAGES["polish.scam.phrase_gate_judge"]["en"]
    assert "does not change the frozen kernel" in en and "not part of the F9" in en


def test_the_preset_is_described_as_a_keyword_rule_that_may_be_wrong():
    zh = UI_MESSAGES["polish.scam.pause_lead_preset"]["zh-CN"]
    assert "关键词" in zh and "可能有误" in zh and "重新作答" in zh
    en = UI_MESSAGES["polish.scam.pause_lead_preset"]["en"]
    assert "keyword" in en and "may be wrong" in en


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("妈妈让我转告爸爸，我给妈妈转500", False),
        ("老板让我汇报一下，给房东转2000元", False),
        ("同事让我转发这个链接，给妈妈转500", False),
        ("对方让我给新联系人转480元", True),
        ("警察叫我马上汇款给新联系人", True),
    ],
)
def test_relaying_a_message_is_not_someone_telling_the_user_to_pay(text, expected):
    assert fraud_hint.directed(text) is expected, text


def test_frozen_corpora_still_never_hit_the_extended_lexicon():
    from tests.unit.test_s5_negation import DATA

    lines = [
        json.loads(line)["text"]
        for line in (DATA / "colloquial.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    for row in json.loads((DATA / "cases.json").read_text(encoding="utf-8")):
        lines.extend(turn["task"] for turn in [*row["request"]["context"], row["request"]])
    assert [text for text in lines if scam_phrase(text)] == []
    assert not [text for text in lines if re.search("公检法|最高检|国安|银监会", text)]
