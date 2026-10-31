"""S6: the display-layer recipient check pauses a gesture; it never changes an outcome.

Every check here is display-only or a fail-closed refusal at a controller entry point:
kernel verdicts, tiers, cards, approvals, dispatch and audit stay identical, and the
TCB is untouched. The gate itself is an explicit per-action state machine
(``ui/scam_gate.py``); refused gestures are proven with spies on every side-effect call
(MFA issue and verification, approval consumption, device signature, gateway dispatch),
not only by their returned status. AppTest flows follow tests/unit/test_ui_fraud_hint.py.
"""

import ast
import json
import re
from collections import Counter
from copy import deepcopy
from pathlib import Path

import pytest

from corridor.agent.domestic_session import DomesticSession
from corridor.channels.im import IMAdapter
from corridor.crypto.jcs import H
from corridor.schemas.domain import Contact
from corridor.ui import fraud_hint, scam_friction, scam_gate
from corridor.ui.controller import DemoController, demo_state
from corridor.ui.messages import UI_MESSAGES, source_utterance
from tests.e2e.test_demo_ui import app, navigate

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "src/corridor/ui/scam_friction.py"
GATE_SOURCE = ROOT / "src/corridor/ui/scam_gate.py"
# R7: "反诈中心让我立即……不然账户冻结" now gets no card (the
# phrase gate: an impersonated authority plus a freeze / transfer-now demand, see
# test_ui_scam_r7.py), and "某某让我转" presets the second question. The flows here keep the
# same cue set with words that still reach the card: "工作人员" is an authority cue of the
# warning but not on the phrase gate's closed list, and "说" tells nobody to pay.
FRAUD = "工作人员说立即给新联系人转480元，不然账户冻结"
NEW = "给新联系人转480元"
CLARIFY = "工作人员说立即给Lin转200元，不然账户冻结"
CLARIFY_PLAIN = "给Lin转200元"
KNOWN_AUTHORITY = "反诈中心说给妈妈转500元"
SCHEDULED_NEW = "10月20日给新联系人转300元"
SAFE = {"verified": "yes", "guided": "no"}
GUIDED = {"verified": "yes", "guided": "yes"}
UNVERIFIED = {"verified": "no", "guided": "no"}
UNSURE = {"verified": "unsure", "guided": "no"}
BLANK = {"verified": None, "guided": None}


def _zh_q_01():
    case = json.loads((ROOT / "data/blind-zh/cases/zh-q-01.json").read_text(encoding="utf-8"))
    return case["turns"][0]["user_text"], case["legit_twin"]["turns"][0]["user_text"]


def _fresh():
    controller = DemoController(initial_state=demo_state(showcase=True))
    controller.start_session()
    return controller, DomesticSession(controller)


def _card(text):
    controller, session = _fresh()
    card = session.receive(text)["card"]
    contact = controller.state.contacts[card["objects"]["contact_ref"]]
    return card, contact


def _gate(controller, identity):
    status = controller.scam_gate_status(identity)
    if status is None:
        return None
    return (status["decision"], status["answers"], status["level"], status["epoch"])


# --- pure functions ---------------------------------------------------------------------


def _imports(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(item.name for item in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.update(f"{node.module}.{item.name}" for item in node.names)
    return imports


def test_imports_are_whitelisted_and_sources_have_no_authority_calls():
    assert _imports(SOURCE) <= {
        "typing.Any",
        "corridor.crypto.jcs.H",
        "corridor.i18n.tr",
        "corridor.ui.display_names",
        "corridor.ui.fraud_hint",
        "corridor.ui.messages.UI_MESSAGES",
        "corridor.ui.messages.tr",
    }
    assert _imports(GATE_SOURCE) <= {
        "copy.deepcopy",
        "typing.Any",
        "corridor.ui.scam_friction",
    }
    for path in (SOURCE, GATE_SOURCE):
        source = path.read_text(encoding="utf-8")
        for word in (
            "streamlit",
            "gateway",
            "kernel",
            "approve",
            "confirm",
            "dispatch",
            "propose",
            "request_mfa",
            "submit",
            "session_state",
            "datetime",
            "time.",
        ):
            assert word not in source, (path.name, word)


def test_level_and_decision_are_pure():
    attack, _ = _zh_q_01()
    card, contact = _card(NEW)
    before = (deepcopy(card), contact.model_copy(deep=True), attack)
    answers = {"verified": "no", "guided": "yes"}
    frozen = deepcopy(answers)
    assert scam_friction.level(card, contact=contact, text=attack) == 2
    assert scam_friction.decision(2, answers) == "pause"
    scam_friction.card_identity(card)
    scam_friction.facts(card, _fresh()[0].state)
    assert (card, contact, attack) == before
    assert answers == frozen


def test_levels_follow_the_fraud_warning_signals():
    attack, twin = _zh_q_01()
    card, contact = _card(NEW)
    assert scam_friction.level(card, contact=contact, text=attack) == 2
    assert scam_friction.cues(card, contact=contact, text=attack) == (
        "authority",
        "external",
        "urgency",
        "split",
    )
    assert scam_friction.level(card, contact=contact, text=None) == 1
    assert scam_friction.level(card, contact=contact, text=NEW) == 1
    # Rule R5: only an authority or external cue raises a new payee to level 2; urgency
    # or split alone leave it at level 1 (the questions and the cue sentence stay), so an
    # everyday word such as 马上 or 分两笔 never pauses an honest "not verified" answer.
    assert scam_friction.level(card, contact=contact, text="马上给新联系人转480元") == 1
    assert scam_friction.cues(card, contact=contact, text="马上给新联系人转480元") == ("urgency",)
    assert scam_friction.level(card, contact=contact, text="给新联系人转480元，房租分两笔") == 1
    assert scam_friction.level(card, contact=contact, text="尽快给新联系人转480元，分两笔") == 1
    external = "给新联系人转480元，账号是对方发来的"
    assert scam_friction.level(card, contact=contact, text=external) == 2
    assert scam_friction.level(card, contact=contact, text="反诈中心让我给新联系人转480元") == 2
    twin_card, twin_contact = _card("给妈妈转500元")
    assert scam_friction.level(twin_card, contact=twin_contact, text=twin) == 0
    assert scam_friction.level(twin_card, contact=twin_contact, text="马上给妈妈转500元") == 0
    # An authority or external cue towards a known contact still asks the questions.
    assert (
        scam_friction.level(twin_card, contact=twin_contact, text="反诈中心让我给妈妈转500元") == 1
    )
    assert scam_friction.level(twin_card, contact=twin_contact, text="对方提供的账户") == 1
    # The file reminder alone is not a recipient signal; a read-only kind never triggers.
    assert scam_friction.level(twin_card, contact=twin_contact, external_files=2) == 0
    assert scam_friction.level({"kind": "cancel_subscription", "tier": "yellow"}) == 0
    assert scam_friction.level({"kind": "balance_query"}, text=attack) == 0
    assert scam_friction.level(None, text=attack) == 0
    assert scam_friction.level("transfer", text=attack) == 0
    assert set(fraud_hint.signals(card, contact=contact, text=attack)) == {
        "new_payee",
        *scam_friction.cues(card, contact=contact, text=attack),
    }


CUE_WORDS = {"authority": "反诈中心", "external": "对方提供", "urgency": "马上", "split": "分两笔"}
LEVEL_TABLE = [
    (new_payee, tuple(cue for bit, cue in enumerate(fraud_hint.TEXT_SIGNALS) if mask & (1 << bit)))
    for new_payee in (True, False)
    for mask in range(1 << len(fraud_hint.TEXT_SIGNALS))
]


def _expected_level(new_payee, codes):
    """An independent statement of the level rule (R5), not the code: a new payee
    with an authority or external cue is level 2; a new payee alone, a new payee with only
    urgency or split cues, or an authority or external cue towards a known contact is
    level 1; urgency or split alone never triggers."""
    strong = bool({"authority", "external"} & set(codes))
    if new_payee and strong:
        return 2
    if new_payee or strong:
        return 1
    return 0


@pytest.fixture(scope="module")
def payee_cards():
    return {True: _card(NEW), False: _card("给妈妈转500元")}


@pytest.mark.parametrize(("new_payee", "codes"), LEVEL_TABLE)
def test_level_truth_table(payee_cards, new_payee, codes):
    card, contact = payee_cards[new_payee]
    words = "".join(CUE_WORDS[code] for code in codes) + (NEW if new_payee else "给妈妈转500元")
    expected = _expected_level(new_payee, codes)
    assert scam_friction.level(card, contact=contact, text=words) == expected
    # The cue sentence lists every matched text rule whenever the questions are asked.
    assert scam_friction.cues(card, contact=contact, text=words) == (codes if expected else ())


def _expected_decision(grade, verified, guided):
    """An independent statement of the rule: the full truth table, not the code."""
    if grade == 0:
        return "proceed"
    if verified is None or guided is None:
        return "incomplete"
    if guided == "yes":
        return "pause"
    if grade == 2 and verified in {"no", "unsure"}:
        return "pause"
    return "proceed"


TRUTH_TABLE = [
    (grade, verified, guided)
    for grade in (0, 1, 2)
    for verified in ("yes", "no", "unsure", None)
    for guided in ("no", "yes", None)
]


@pytest.mark.parametrize(("grade", "verified", "guided"), TRUTH_TABLE)
def test_decision_truth_table(grade, verified, guided):
    answers = {key: value for key, value in (("verified", verified), ("guided", guided))}
    expected = _expected_decision(grade, verified, guided)
    assert scam_friction.decision(grade, answers) == expected
    assert expected in scam_friction.DECISIONS
    # The triggering answers (for the pause copy) are named independently of the other
    # question; the decision itself is "incomplete" until both are answered.
    triggered = scam_friction.triggers(grade, answers)
    if expected != "incomplete":
        assert (expected == "pause") == bool(triggered)
    if grade == 2 and verified in {"no", "unsure"}:
        assert f"verified_{verified}" in triggered
    if grade == 1:
        assert all(code.startswith("guided") for code in triggered)
    if grade == 0:
        assert triggered == ()


def test_decision_rejects_unknown_options_and_non_dicts():
    assert scam_friction.decision(2, "yes") == "incomplete"
    assert scam_friction.decision(2, {"verified": "maybe", "guided": "no"}) == "incomplete"
    assert scam_friction.decision(1, {"verified": "yes"}) == "incomplete"
    assert scam_friction.decision(1, {"verified": "self", "guided": "no"}) == "incomplete"
    assert scam_friction.clean_answers({"verified": "yes", "guided": "no", "x": 1}) == SAFE
    assert scam_friction.LEVEL_ONE_PAUSE == ("guided",)
    assert scam_friction.PAUSE_ANSWERS == {"verified": ("no", "unsure"), "guided": ("yes",)}


def test_card_identity_ignores_only_the_draft_digest():
    controller, session = _fresh()
    first = session.receive(NEW)
    draft_card = first["card"]
    # C2 (review of R7): a new payee's check is now established at the first gesture even
    # when no page drew it, so the gesture to the prepared card answers it first, as the
    # page does (this test is about the card identity, not about the gate).
    identity = first["payload"]["draft_id"]
    status = controller.scam_gate_establish(identity, text=NEW)
    controller.scam_gate_answer(
        identity, card_identity=status["card_identity"], epoch=status["epoch"], answers=SAFE
    )
    prepared_card = session.confirm(first["response_id"])["card"]
    assert draft_card != prepared_card and H(draft_card) != H(prepared_card)
    assert set(draft_card) ^ set(prepared_card) <= {"dependency_hash"}
    assert [key for key in draft_card if draft_card[key] != prepared_card.get(key)] == [
        "dependency_hash"
    ]
    assert scam_friction.card_identity(draft_card) == scam_friction.card_identity(prepared_card)
    # Every field but the draft digest is bound: change any one and the identity moves.
    for key in draft_card:
        if key == "dependency_hash":
            continue
        changed = {**draft_card, key: "changed" if not isinstance(draft_card[key], dict) else {}}
        assert scam_friction.card_identity(changed) != scam_friction.card_identity(draft_card), key
    other_payee = {
        **draft_card,
        "objects": {**draft_card["objects"], "contact_ref": "CONTACT_CHEN"},
    }
    assert scam_friction.card_identity(other_payee) != scam_friction.card_identity(draft_card)
    assert scam_friction.card_identity({}) is None and scam_friction.card_identity(None) is None
    # A card set: one card gives its own identity; several give one identity for all.
    assert scam_friction.cards_identity([draft_card]) == scam_friction.card_identity(draft_card)
    both = scam_friction.cards_identity([draft_card, other_payee])
    assert both == scam_friction.cards_identity([other_payee, draft_card])
    assert both not in {scam_friction.card_identity(draft_card), None}
    for unusable in ([], None, [{}], [draft_card, {}], "card", [None]):
        assert scam_friction.cards_identity(unusable) is None, unusable


@pytest.mark.parametrize("locale", ["zh-CN", "en"])
def test_copy_never_claims_to_block_or_detect_fraud(locale):
    keys = [key for key in UI_MESSAGES if key.removeprefix("customer:").startswith("polish.scam.")]
    assert len(keys) > 40
    for key in keys:
        text = UI_MESSAGES[key][locale]
        if locale == "zh-CN":
            # The judge note quotes the fraud warning's own footer ("不会拦截、延迟") only to
            # say what that sentence refers to; nothing else may use the word.
            stripped = text.replace("“不会拦截、延迟”", "")
            for banned in (
                "拦截",
                "识别诈骗",
                "识别为诈骗",
                "保证",
                "零副作用",
                "延迟的是",
                "风控",
                "核验通过",
                "已拦下",
                "放行",
                "没有发起",
                "不进入状态",
                "第一次付款",
                "首次付款",
            ):
                assert banned not in stripped, (key, banned)
        else:
            lowered = text.lower().replace("does not block or delay", "")
            for banned in (
                "block",
                "intercept",
                "detect",
                "guarantee",
                "zero side effect",
                "risk control",
                "catch rate",
                "has been requested",
                "nothing has been submitted",
                "first payment",
            ):
                assert banned not in lowered, (key, banned)
        # No invented number; R7's ruled anti-fraud copy names the public national
        # anti-fraud hotline 96110, which is not synthetic data.
        assert not re.search(r"\d{5,}", text.replace("96110", "")), key


def test_copy_separates_the_users_check_from_the_banks_verification():
    """用户做的事叫“核实”，银行做的事叫“核验”；标题不得把核验当成用户动作。"""
    zh = {key: UI_MESSAGES[key]["zh-CN"] for key in UI_MESSAGES if key.startswith("polish.scam.")}
    assert zh["polish.scam.title"].startswith("请您核实收款人")
    assert "核验" not in zh["polish.scam.title"]
    for key in ("polish.scam.incomplete_copy", "polish.scam.paused_copy", "polish.scam.advice"):
        assert "核验" not in zh[key], key
    assert zh["polish.scam.fact_source"].endswith("不是银行对对方身份的核验。")
    assert "不构成银行核验" in zh["polish.scam.pause_self_report"]
    assert (
        UI_MESSAGES["Awaiting the recipient check answers"]["zh-CN"] == "等待您作答（核实收款人）"
    )
    # Each wording item of the second review round, in both languages.
    assert "仅保存在当前产品会话，不进入规范卡、签名载荷或审计链" in zh["polish.scam.not_signed"]
    assert "不改变冻结内核原有的审批与签名规则" in zh["polish.scam.not_signed"]
    assert "本界面另有作答前置条件" in zh["polish.scam.not_signed"]
    en_not_signed = UI_MESSAGES["polish.scam.not_signed"]["en"]
    assert "only in the current product session" in en_not_signed
    assert "frozen kernel's own approval and signing rules" in en_not_signed
    footer = UI_MESSAGES["polish.scam.footer"]
    assert "是否继续由您决定" in footer["zh-CN"] and "不判断收款人是否为诈骗方" in footer["zh-CN"]
    assert (
        "在两题回答完整且这一步自查允许继续之前，本界面不会再发起新的短信验证、确认审批或付款提交；"
        "它可能暂停确认流程，从而推迟或避免交易发起" in footer["zh-CN"]
    )
    assert "your decision" in footer["en"] and "delay or avoid initiating" in footer["en"]
    note = UI_MESSAGES["polish.scam.judge_note"]
    assert "尚无诈骗识别或减少损失的效果证据" in note["zh-CN"]
    assert "no evidence yet that it identifies scams or reduces losses" in note["en"]
    assert "仍按原有效期过期；过期后需取消重来" in note["zh-CN"]
    assert "指提醒本身" in note["zh-CN"] and "不是路线图中的内核冷静期" in note["zh-CN"]
    assert "F9" in note["zh-CN"] and "F9" in note["en"]
    assert "附件数量" in note["zh-CN"] and "attachment count" in note["en"]
    challenge = UI_MESSAGES["polish.scam.pause_challenge"]
    assert "此前发出的验证码仍按原有效期计时；过期后需取消并重新发起" in challenge["zh-CN"]
    assert "original validity window" in challenge["en"]
    assert zh["polish.scam.fact_new"] == "登记状态：该收款人登记为新收款人。"
    assert "registered as a new recipient" in UI_MESSAGES["polish.scam.fact_new"]["en"]
    assert (
        "门控状态＝" in zh["polish.scam.judge_record"]
        and "决定＝" not in zh["polish.scam.judge_record"]
    )
    assert "gate state" in UI_MESSAGES["polish.scam.judge_record"]["en"]
    assert "设备页" not in zh["polish.scam.other_gated"]
    assert "device page" not in UI_MESSAGES["polish.scam.other_gated"]["en"]
    for key in ("polish.scam.incomplete", "polish.scam.device_incomplete"):
        assert "不会再发起新的短信验证" in zh[key], key
        assert "new sms verification" in UI_MESSAGES[key]["en"].lower(), key
    assert zh["polish.scam.drill_caption"].startswith("一键向助手提交一条模拟付款请求")
    assert (
        "submits a simulated payment request to the assistant"
        in (UI_MESSAGES["polish.scam.drill_caption"]["en"])
    )
    assert (
        "命中了固定关键词或措辞规则" in zh["polish.scam.cues"]
        and "识别" not in zh["polish.scam.cues"]
    )
    assert "matches fixed keyword or phrasing rules" in UI_MESSAGES["polish.scam.cues"]["en"]
    assert "独立可信渠道" in zh["polish.scam.q_verified"]
    assert zh["polish.scam.q_verified.yes"] == "已独立核实"
    assert zh["polish.scam.q_verified.no"] == "未独立核实（只有对方提供的信息）"
    assert zh["polish.scam.q_verified.unsure"] == "不确定"
    assert zh["polish.scam.q_guided"] == "现在是否有人在电话或聊天中指导您转账？"
    for key in ("polish.scam.pause_retry", "polish.scam.pause_retry_guided"):
        assert "用户自述" in zh[key], key
        assert "self-reported" in UI_MESSAGES[key]["en"], key
    # Future tense only, with the accurate recovery condition: a pause may follow an
    # issued challenge, which is kept, and it ends only when the check allows continuing.
    for key in (
        "polish.scam.pause",
        "polish.scam.pause_authority",
        "polish.scam.pause_guided",
        "polish.scam.pause_guided_authority",
        "polish.scam.paused_copy",
    ):
        assert "不会再发起新的短信验证" in zh[key], key
        assert "在两题回答完整且这一步自查允许继续之前" in zh[key], key
        assert "重新作答之前" not in zh[key], key
        assert "new sms verification" in UI_MESSAGES[key]["en"].lower(), key
        assert "check allows you to continue" in UI_MESSAGES[key]["en"], key
    assert "挂断电话" in zh["polish.scam.pause_authority"]
    assert "挂断电话" not in zh["polish.scam.pause"]
    assert "银行、公安" not in zh["polish.scam.pause_authority"]
    # R5: a pause by "someone guiding" alone asks the user to end the guidance and decide
    # alone; "verify the account through an official channel" is not its remedy. With an
    # authority cue the official-number line stays; the fraud warning on the same screen
    # already says that banks, police and anti-fraud centres never ask for a transfer.
    for key in (
        "polish.scam.pause_title_guided",
        "polish.scam.pause_retry_guided",
        "polish.scam.pause_guided",
    ):
        assert "官方" not in zh[key] and "核实" not in zh[key], key
        assert "official" not in UI_MESSAGES[key]["en"].lower(), key
    assert "挂断或结束对方的指导" in zh["polish.scam.pause_guided"]
    assert "是您本人的意愿" in zh["polish.scam.pause_guided"]
    assert zh["polish.scam.pause_title_guided"].startswith("已暂停：")
    assert zh["polish.scam.pause_retry_guided"] == "我已独立确认（用户自述），重新作答"
    assert "挂断电话" in zh["polish.scam.pause_guided_authority"]
    assert "银行卡背面的官方客服电话" in zh["polish.scam.pause_guided_authority"]
    assert "银行、公安" not in zh["polish.scam.pause_guided_authority"]
    assert "official number" in UI_MESSAGES["polish.scam.pause_guided_authority"]["en"]
    # R6: the button for that branch states both what the action sentence asks for:
    # confirmed on my own, and verified by the official number; still only a self-report.
    assert (
        zh["polish.scam.pause_retry_guided_authority"]
        == "我已独立确认并通过官方电话核实（用户自述），重新作答"
    )
    guided_authority_en = UI_MESSAGES["polish.scam.pause_retry_guided_authority"]["en"]
    assert "on my own" in guided_authority_en and "official number" in guided_authority_en
    assert "self-reported" in guided_authority_en
    # R6: the customer's three titles say 页面提示 / "a page prompt", never 展示层.
    for key in ("polish.scam.title", "polish.scam.facts_title", "polish.scam.device_title"):
        plain_title = UI_MESSAGES["customer:" + key]
        assert "展示层" not in plain_title["zh-CN"] and "display" not in plain_title["en"].lower()
        assert "不属于签名内容" in plain_title["zh-CN"] or "只读" in plain_title["zh-CN"], key
    assert "页面提示" in UI_MESSAGES["customer:polish.scam.title"]["zh-CN"]
    assert "页面提示" in UI_MESSAGES["customer:polish.scam.device_title"]["zh-CN"]
    assert "a page prompt" in UI_MESSAGES["customer:polish.scam.title"]["en"]
    # R5: 门控 is a judge's word; every customer-visible line says it plainly (the English
    # already says "this check"), and the judge's isolation paragraph has a plain customer
    # version without the kernel's vocabulary.
    judge_only = {
        "polish.scam.judge_note",
        "polish.scam.judge_record",
        "polish.scam.drill_caption_judge",
    }
    for key in zh:
        if key not in judge_only:
            assert "门控" not in zh[key], key
    plain = UI_MESSAGES["customer:polish.scam.not_signed"]
    assert "两个问题" in plain["zh-CN"] and "不属于签名内容" in plain["zh-CN"]
    for jargon in ("门控", "规范卡", "签名载荷", "审计链", "冻结内核", "前置条件", "展示层"):
        assert jargon not in plain["zh-CN"], jargon
    assert "two questions" in plain["en"] and "signed content" in plain["en"]
    for jargon in ("canonical card", "signature payload", "audit chain", "kernel"):
        assert jargon not in plain["en"].lower(), jargon
    # R5: Q1's examples include a non-impersonation channel (in person, a number of
    # theirs saved long ago), and the official number only for a claimed authority.
    for key in ("polish.scam.q_verified", "polish.scam.advice"):
        assert "当面" in zh[key] and "早已保存的对方电话" in zh[key], key
        assert "银行卡背面的官方电话" in zh[key], key
        assert "in person" in UI_MESSAGES[key]["en"] and "saved long ago" in UI_MESSAGES[key]["en"]
    # R5: the fraud warning's own first line says what the registry says (a registration
    # label), not a payment history; this line is not the warning's footer.
    first_line = UI_MESSAGES["polish.fraud.new_payee"]
    assert first_line["zh-CN"] == "「{payee}」登记为新收款人。"
    assert first_line["en"] == "{payee} is registered as a new recipient."
    assert "可信通讯录" in zh["polish.scam.facts_title"]
    assert zh["polish.scam.decision.proceed"] == "可继续"
    assert "zh-q-01" not in zh["polish.scam.drill_caption"]
    assert "zh-q-01" in zh["polish.scam.drill_caption_judge"]
    assert "no payment happens unless you act" in UI_MESSAGES["polish.scam.drill_caption"]["en"]
    assert "您在对话页的回答触发了暂停" in zh["polish.scam.device_paused"]


@pytest.mark.parametrize("locale", ["zh-CN", "en"])
def test_pause_copy_names_only_the_answers_that_triggered_it(monkeypatch, locale):
    monkeypatch.setenv("CORRIDOR_LOCALE", locale)
    from corridor.i18n import set_locale

    set_locale(locale)
    msg = {key: UI_MESSAGES[key][locale] for key in UI_MESSAGES if key.startswith("polish.scam.")}
    guided_only = scam_friction.pause_copy((), GUIDED, 1)
    assert msg["polish.scam.trigger.guided_yes"] in guided_only
    assert msg["polish.scam.trigger.verified_no"] not in guided_only
    assert msg["polish.scam.trigger.verified_unsure"] not in guided_only
    unverified = scam_friction.pause_copy(("external",), UNVERIFIED, 2)
    assert msg["polish.scam.trigger.verified_no"] in unverified
    assert msg["polish.scam.trigger.guided_yes"] not in unverified
    unsure = scam_friction.pause_copy(("authority",), UNSURE, 2)
    assert msg["polish.scam.trigger.verified_unsure"] in unsure
    both = scam_friction.pause_copy(("authority",), {"verified": "no", "guided": "yes"}, 2)
    assert msg["polish.scam.trigger.verified_no"] in both
    assert msg["polish.scam.trigger.guided_yes"] in both
    assert msg["polish.scam.trigger_separator"] in both
    # Level 1 ignores the verification answer: "not verified" is not named as a trigger.
    level_one = scam_friction.pause_copy((), {"verified": "no", "guided": "yes"}, 1)
    assert msg["polish.scam.trigger.verified_no"] not in level_one
    assert msg["polish.scam.trigger.guided_yes"] in level_one
    # The hang-up line only with an authority cue; the challenge note only on request.
    assert ("挂断电话" in unsure) == (locale == "zh-CN")
    assert ("Hang up first" in unsure) == (locale == "en")
    assert msg["polish.scam.pause_challenge"] not in guided_only
    assert msg["polish.scam.pause_challenge"] in scam_friction.pause_copy(
        (), GUIDED, 1, challenge=True
    )

    # R5: a pause triggered by "someone guiding" alone speaks of ending the guidance and
    # deciding alone, never of verifying through an official channel; with an authority
    # cue the official-number line stays. Any verification trigger (alone or together
    # with guiding) keeps the verification wording, title and button.
    def tail(key):
        return msg[key].split("{lead}")[-1]

    assert tail("polish.scam.pause_guided") in guided_only
    assert tail("polish.scam.pause") not in guided_only
    guided_authority = scam_friction.pause_copy(("authority",), GUIDED, 2)
    assert tail("polish.scam.pause_guided_authority") in guided_authority
    assert tail("polish.scam.pause_guided") not in guided_authority
    assert tail("polish.scam.pause_authority") in both
    assert tail("polish.scam.pause_guided_authority") not in both
    assert tail("polish.scam.pause") in scam_friction.pause_copy((), UNVERIFIED, 2)
    assert tail("polish.scam.pause_guided") in level_one
    mixed = {"verified": "unsure", "guided": "yes"}
    assert scam_friction.guided_only(1, GUIDED) and scam_friction.guided_only(2, GUIDED)
    assert scam_friction.guided_only(1, mixed) and not scam_friction.guided_only(2, mixed)
    assert not scam_friction.guided_only(2, UNVERIFIED) and not scam_friction.guided_only(2, None)
    assert scam_friction.pause_title(1, GUIDED) == msg["polish.scam.pause_title_guided"]
    assert scam_friction.pause_title(2, GUIDED) == msg["polish.scam.pause_title_guided"]
    assert scam_friction.pause_title(2, UNVERIFIED) == msg["polish.scam.pause_title"]
    assert scam_friction.pause_title(2, mixed) == msg["polish.scam.pause_title"]
    assert scam_friction.pause_retry_label(1, GUIDED) == msg["polish.scam.pause_retry_guided"]
    assert scam_friction.pause_retry_label(2, UNSURE) == msg["polish.scam.pause_retry"]
    assert scam_friction.pause_retry_label(2, mixed) == msg["polish.scam.pause_retry"]
    # R6: when guiding alone paused it but the user's words carried an authority cue, the
    # action sentence also asks for the official-number check, so the only answer-again
    # button covers both statements; without the cue, or with a verification trigger, the
    # buttons are the R5 ones.
    authority = msg["polish.scam.pause_retry_guided_authority"]
    assert scam_friction.pause_retry_label(1, GUIDED, ("authority",)) == authority
    assert scam_friction.pause_retry_label(2, GUIDED, ("authority", "urgency")) == authority
    assert (
        scam_friction.pause_retry_label(2, GUIDED, ("external",))
        == msg["polish.scam.pause_retry_guided"]
    )
    assert (
        scam_friction.pause_retry_label(2, UNSURE, ("authority",)) == msg["polish.scam.pause_retry"]
    )
    assert (
        scam_friction.pause_retry_label(2, mixed, ("authority",)) == msg["polish.scam.pause_retry"]
    )
    assert authority != msg["polish.scam.pause_retry_guided"]
    set_locale(None)


def test_drill_utterance_matches_the_zh_q_01_cue_set_and_is_held_by_the_phrase_gate():
    # R7: the drill (zh-q-01 family) hits both closed lists of the phrase gate, so it now
    # gets no card; its cue set on the same new-payee card still equals zh-q-01's.
    attack, _ = _zh_q_01()
    drill = source_utterance("polish.scam.drill_utterance")
    controller, session = _fresh()
    response = session.receive(drill)
    assert response["status"] == "clarify" and not response["card"]
    assert response["payload"]["scam_phrase"]["authority"] == ["反诈中心"]
    assert not controller._drafts and controller.gateway.bank.execute_count == 0
    tokenized = controller.gateway.vault.tokenize(drill)
    card, contact = _card(NEW)
    assert contact.id == "CONTACT_NEW" and card["tier_reasons"] == ["new_recipient"]
    assert set(fraud_hint.signals(card, contact=contact, text=tokenized)) == set(
        fraud_hint.signals(card, contact=contact, text=attack)
    )
    assert scam_friction.level(card, contact=contact, text=tokenized) == 2


# --- the state machine on its own -------------------------------------------------------


def _context(text="t", files=0):
    return {"text_hash": text, "external_files": files, "contacts": [{"id": "C"}]}


def test_gate_establish_is_idempotent_and_regrades_only_on_a_change():
    gate = scam_gate.ScamGate()
    first = gate.establish("a", card_identity="card-1", level=2, context=_context(), cues=("x",))
    assert first["required"] and first["epoch"] == 0 and first["decision"] == "incomplete"
    assert first["answers"] == BLANK and first["level"] == 2 and first["cues"] == ("x",)
    assert gate.answer("a", card_identity="card-1", epoch=0, answers=SAFE) is None
    assert gate.entry("a")["decision"] == "proceed"
    # The same card and context: nothing changes, and a lower level passed by an
    # ordinary redraw is ignored.
    again = gate.establish("a", card_identity="card-1", level=1, context=_context())
    assert again["level"] == 2 and again["epoch"] == 0 and again["decision"] == "proceed"
    assert again["answers"] == SAFE
    # A context change is an explicit re-grade: answers invalidated, epoch advanced.
    regraded = gate.establish("a", card_identity="card-1", level=1, context=_context(files=1))
    assert regraded["level"] == 1 and regraded["epoch"] == 1
    assert regraded["decision"] == "incomplete" and regraded["answers"] == BLANK
    assert regraded["regrades"] == 1 and regraded["required"]
    # A card change likewise; the old epoch's answers can no longer be applied.
    assert gate.answer("a", card_identity="card-1", epoch=1, answers=SAFE) is None
    rebound = gate.establish("a", card_identity="card-2", level=2, context=_context(files=1))
    assert rebound["card_identity"] == "card-2" and rebound["epoch"] == 2
    assert rebound["decision"] == "incomplete"
    assert gate.answer("a", card_identity="card-1", epoch=1, answers=SAFE) == "card_mismatch"
    assert gate.entry("a")["decision"] == "incomplete"
    assert "a" in gate.identities()


def test_gate_refuses_stale_events_without_side_effects_and_records_why():
    gate = scam_gate.ScamGate()
    assert gate.answer("none", card_identity="c", epoch=0, answers=SAFE) == "no_gate"
    assert gate.retry("none", card_identity="c", epoch=0) == "no_gate"
    assert gate.entry("none") is None
    gate.establish("a", card_identity="c", level=2, context=_context())
    assert gate.answer("a", card_identity="other", epoch=0, answers=SAFE) == "card_mismatch"
    assert gate.answer("a", card_identity="c", epoch=1, answers=SAFE) == "epoch_mismatch"
    assert gate.retry("a", card_identity="c", epoch=0) == "not_paused"
    assert gate.entry("a")["answers"] == BLANK and gate.entry("a")["epoch"] == 0
    # A pause latches: answers for that epoch are refused until a retry.
    assert gate.answer("a", card_identity="c", epoch=0, answers=UNVERIFIED) is None
    assert gate.entry("a")["decision"] == "pause"
    assert gate.answer("a", card_identity="c", epoch=0, answers=SAFE) == "latched"
    assert gate.entry("a")["answers"] == UNVERIFIED
    assert gate.retry("a", card_identity="c", epoch=1) == "epoch_mismatch"
    assert gate.retry("a", card_identity="x", epoch=0) == "card_mismatch"
    reasons = [item["reason"] for item in gate.rejections()]
    assert reasons == [
        "no_gate",
        "no_gate",
        "card_mismatch",
        "epoch_mismatch",
        "not_paused",
        "latched",
        "epoch_mismatch",
        "card_mismatch",
    ]
    assert all(item["identity"] in {"a", "none"} for item in gate.rejections())


def test_gate_retry_keeps_the_entry_and_opens_a_new_epoch():
    gate = scam_gate.ScamGate()
    gate.establish("a", card_identity="c", level=1, context=_context())
    gate.answer("a", card_identity="c", epoch=0, answers=GUIDED)
    assert gate.refusal_reason("a", "c") == "scam_pause"
    assert gate.retry("a", card_identity="c", epoch=0) is None
    entry = gate.entry("a")
    assert entry["epoch"] == 1 and entry["retries"] == 1 and entry["required"]
    assert entry["answers"] == BLANK and entry["decision"] == "incomplete"
    assert entry["level"] == 1 and entry["card_identity"] == "c"
    assert gate.refusal_reason("a", "c") == "scam_incomplete"
    # The old epoch's retry button and answers are dead; the new epoch works.
    assert gate.retry("a", card_identity="c", epoch=0) == "epoch_mismatch"
    assert gate.answer("a", card_identity="c", epoch=0, answers=SAFE) == "epoch_mismatch"
    assert gate.answer("a", card_identity="c", epoch=1, answers=SAFE) is None
    assert gate.refusal_reason("a", "c") is None


def test_gate_refusal_reasons_cover_stale_incomplete_and_pause():
    gate = scam_gate.ScamGate()
    assert gate.refusal_reason("a", "c") is None
    assert gate.refusal_reason("a", None) is None
    gate.establish("a", card_identity="c", level=2, context=_context())
    assert gate.refusal_reason("a", "c") == "scam_incomplete"
    assert gate.refusal_reason("a", "other") == "scam_stale"
    assert gate.refusal_reason("a", None) == "scam_stale"
    gate.answer("a", card_identity="c", epoch=0, answers=UNSURE)
    assert gate.refusal_reason("a", "c") == "scam_pause"
    assert gate.refusal_reason("a", "other") == "scam_stale"
    gate.retry("a", card_identity="c", epoch=0)
    gate.answer("a", card_identity="c", epoch=1, answers=SAFE)
    assert gate.refusal_reason("a", "c") is None
    assert gate.refusal_reason("a", None) == "scam_stale"
    assert scam_gate.REASONS == ("scam_incomplete", "scam_pause", "scam_stale")


def test_gate_copies_never_alias_the_record():
    gate = scam_gate.ScamGate()
    context = _context()
    gate.establish("a", card_identity="c", level=2, context=context)
    context["external_files"] = 9
    assert gate.entry("a")["context"]["external_files"] == 0
    given = {"verified": "yes", "guided": "no"}
    gate.answer("a", card_identity="c", epoch=0, answers=given)
    given["guided"] = "yes"
    assert gate.entry("a")["answers"] == SAFE and gate.entry("a")["decision"] == "proceed"
    copy = gate.entry("a")
    copy["answers"]["guided"] = "yes"
    copy["decision"] = "pause"
    assert gate.entry("a")["answers"] == SAFE and gate.entry("a")["decision"] == "proceed"
    origin = {"prefix": "domestic", "response_id": "r"}
    gate.establish("b", card_identity="c", level=1, context=_context(), origin=origin)
    origin["response_id"] = "x"
    assert gate.entry("b")["origin"] == {"prefix": "domestic", "response_id": "r"}


def test_gate_level_never_drops_to_zero_once_established():
    """Third-round finding P1: a re-grade whose fresh grade is 0 (the words went
    missing, the payee switched to a known contact) is stored as level 1 with blank
    answers and ``incomplete``; the machine has no blank "proceed" record at all."""
    gate = scam_gate.ScamGate()
    assert scam_gate.MIN_LEVEL == 1
    gate.establish("a", card_identity="card-1", level=1, context=_context(), cues=("authority",))
    gate.answer("a", card_identity="card-1", epoch=0, answers=GUIDED)
    assert gate.refusal_reason("a", "card-1") == "scam_pause"
    # The same card, a context whose grade is 0: still a gate, still unanswered.
    regraded = gate.establish("a", card_identity="card-1", level=0, context=_context(text=None))
    assert regraded["level"] == 1 and regraded["decision"] == "incomplete"
    assert regraded["answers"] == BLANK and regraded["epoch"] == 1 and regraded["required"]
    assert regraded["regrades"] == 1 and regraded["cues"] == ()
    assert gate.refusal_reason("a", "card-1") == "scam_incomplete"
    # A card change computed as level 0 likewise; the old card reads as stale.
    rebound = gate.establish("a", card_identity="card-2", level=0, context=_context(text=None))
    assert rebound["level"] == 1 and rebound["decision"] == "incomplete" and rebound["epoch"] == 2
    assert gate.refusal_reason("a", "card-2") == "scam_incomplete"
    assert gate.refusal_reason("a", "card-1") == "scam_stale"
    # The old epochs' answers cannot be replayed; level-1 rules judge the new ones.
    assert gate.answer("a", card_identity="card-1", epoch=0, answers=SAFE) == "card_mismatch"
    assert gate.answer("a", card_identity="card-2", epoch=1, answers=SAFE) == "epoch_mismatch"
    assert gate.entry("a")["decision"] == "incomplete"
    assert gate.answer("a", card_identity="card-2", epoch=2, answers=GUIDED) is None
    assert gate.refusal_reason("a", "card-2") == "scam_pause"
    assert gate.retry("a", card_identity="card-2", epoch=2) is None
    assert gate.answer("a", card_identity="card-2", epoch=3, answers=UNVERIFIED) is None
    assert gate.refusal_reason("a", "card-2") is None
    # Even a first establishment at level 0 asks (the controller never establishes one
    # for level 0; the machine itself cannot hold a gate that proceeds unanswered).
    fresh = gate.establish("b", card_identity="c", level=0, context=_context())
    assert fresh["level"] == 1 and fresh["decision"] == "incomplete" and fresh["answers"] == BLANK
    assert gate.refusal_reason("b", "c") == "scam_incomplete"


def test_gate_inherit_carries_the_gate_to_a_replacement_identity():
    """Third-round finding P4: when the action instance a gate protects is replaced
    under a new identity, the new identity starts gated (incomplete, the source's
    level, cues, context and origin, its own card); the source entry is untouched."""
    gate = scam_gate.ScamGate()
    assert gate.inherit("none", "new", card_identity="n") is None
    assert gate.entry("new") is None and gate.identities() == []
    origin = {"prefix": "domestic", "response_id": "r", "route": "conversation"}
    gate.establish(
        "old", card_identity="c1", level=2, context=_context(), cues=("authority",), origin=origin
    )
    gate.answer("old", card_identity="c1", epoch=0, answers=SAFE)
    assert gate.refusal_reason("old", "c1") is None
    inherited = gate.inherit("old", "new", card_identity="c2")
    assert inherited["inherited_from"] == "old" and inherited["required"]
    assert inherited["level"] == 2 and inherited["cues"] == ("authority",)
    assert inherited["context"] == _context() and inherited["origin"] == origin
    assert inherited["card_identity"] == "c2" and inherited["epoch"] == 0
    assert inherited["answers"] == BLANK and inherited["decision"] == "incomplete"
    assert inherited["retries"] == 0 and inherited["regrades"] == 0
    assert gate.refusal_reason("new", "c2") == "scam_incomplete"
    assert gate.refusal_reason("new", "c1") == "scam_stale"
    assert gate.refusal_reason("new", None) == "scam_stale"
    assert set(gate.identities()) == {"old", "new"}
    # The source keeps its answers; they never apply to the new identity.
    assert gate.entry("old")["answers"] == SAFE and gate.entry("old")["decision"] == "proceed"
    assert gate.answer("new", card_identity="c1", epoch=0, answers=SAFE) == "card_mismatch"
    assert gate.entry("new")["decision"] == "incomplete"
    # Copies, not aliases; inheriting onto an identity that has a gate changes nothing.
    gate.entry("old")["origin"]["response_id"] = "x"
    assert gate.entry("new")["origin"] == origin
    gate.answer("new", card_identity="c2", epoch=0, answers=UNSURE)
    assert gate.entry("new")["decision"] == "pause"
    assert gate.inherit("old", "new", card_identity="c3")["decision"] == "pause"
    assert gate.entry("new")["card_identity"] == "c2"
    # A replacement whose card has no identity yet is stale (never "no gate").
    assert gate.inherit("old", "blank", card_identity=None)["decision"] == "incomplete"
    assert gate.refusal_reason("blank", None) == "scam_stale"
    assert gate.refusal_reason("blank", "c9") == "scam_stale"


# --- AppTest helpers --------------------------------------------------------------------


def gate_keys(page, prefix, identifier):
    """The current radio keys of the recipient check for one chat response."""
    stem = f"{prefix}_scam:{identifier}:"
    keys = {}
    for element in page.radio:
        if element.key.startswith(stem):
            keys[element.key.rsplit(":", 1)[-1]] = element.key
    return keys


def gate_values(page, prefix, identifier):
    """The answers currently held by the radio widgets (None until the user chooses)."""
    keys = gate_keys(page, prefix, identifier)
    assert set(keys) == {"verified", "guided"}, keys
    return {
        question: page.session_state[key] if key in page.session_state else None
        for question, key in keys.items()
    }


def gate_disabled(page, prefix, identifier):
    stem = f"{prefix}_scam:{identifier}:"
    return {
        element.key.rsplit(":", 1)[-1]: bool(element.disabled)
        for element in page.radio
        if element.key.startswith(stem)
    }


def answer_gate(page, prefix, identifier, *, answers=SAFE):
    keys = gate_keys(page, prefix, identifier)
    assert set(keys) == {"verified", "guided"}, keys
    for question, value in answers.items():
        page.radio(key=keys[question]).set_value(value)
    page.run()
    assert not page.exception


def retry_gate(page):
    retry = next(key for key in button_keys(page) if key.endswith(":retry"))
    page.button(key=retry).click().run()
    assert not page.exception


def button_keys(page):
    return {button.key for button in page.button}


def prune(page):
    """Drop the widgets AppTest keeps from a run that ended in ``st.rerun()``.

    The browser prunes elements the second pass did not draw, but AppTest's tree keeps
    them. Streamlit already dropped their widget state, so on the next ``run()`` a kept
    radio raises and a kept button (sent with an unclicked trigger that differs from its
    missing old state) fires its callback; a widget drawn in both passes is kept twice
    and ``page.button(key=...)`` would return the stale first copy. The real page never
    shows or sends such a widget; only the last copy of each key is kept here.
    """
    from streamlit.testing.v1.element_tree import Block, Widget

    def drop_stateless(block):
        for index, child in list(block.children.items()):
            if isinstance(child, Widget) and child.key and child.key not in page.session_state:
                del block.children[index]
            elif isinstance(child, Block):
                drop_stateless(child)

    drop_stateless(page._tree)
    found = []

    def collect(block):
        for index, child in list(block.children.items()):
            if isinstance(child, Widget) and child.key:
                found.append((block, index, child.key))
            elif isinstance(child, Block):
                collect(child)

    collect(page._tree)
    last = {key: position for position, (_block, _index, key) in enumerate(found)}
    for position, (block, index, key) in enumerate(found):
        if last[key] != position:
            del block.children[index]


def send_message(page, text, *, key="domestic"):
    """Send a chat message (``submit_request`` ends in ``st.rerun()``; see ``prune``)."""
    prune(page)
    page.text_input(key=f"{key}_text").set_value(text)
    page.button(key=f"{key}_send").click().run()
    assert not page.exception
    prune(page)


def shown_text(page):
    groups = (page.markdown, page.caption, page.warning, page.info, page.success, page.error)
    return "\n".join(str(item.value) for group in groups for item in group)


def snapshot(controller, session):
    """Everything a pause must leave untouched; compared before and after the gate."""
    gateway = controller.gateway
    principal = controller.device.principal
    state = controller.state
    return {
        "audit_events": tuple(row["event"] for row in gateway.audit.records()),
        "audit_head": gateway.audit.checkpoint()["head"],
        "state": H(state),
        "clock": state.now,
        "challenges": tuple(sorted(gateway.mfa._challenges)),
        "attempts": tuple(
            (key, value.attempts, value.closed)
            for key, value in sorted(gateway.mfa._challenges.items())
        ),
        "pending": tuple(
            (key, value.get("challenge_id"), value.get("verified"))
            for key, value in sorted(controller._pending.items())
        ),
        "drafts": tuple(sorted(controller._drafts)),
        "results": tuple(sorted(controller._results)),
        "requests": tuple(sorted(controller._requests)),
        "request_facts": tuple(sorted(controller._request_facts.items())),
        "cards": tuple(
            H(value["display"].card)
            for _key, value in sorted(controller._pending.items())
            if value.get("mode") == "prepared"
        ),
        "budget": gateway.approval_budget(principal),
        "consumed_nonces": tuple(sorted(state.security.consumed_nonces)),
        "consumed_grants": tuple(sorted(state.security.consumed_grant_ids)),
        "user_inputs": tuple(sorted(state.user_inputs)),
        "receipts": tuple(sorted(state.receipts)),
        "bank": H(gateway.bank.final_state()),
        "execute_count": gateway.bank.execute_count,
        "safety": gateway.safety_snapshot(principal)["state"],
        "tool_calls": session.tool_calls,
        "history": tuple(row["utterance_hash"] for row in session.history),
    }


def _without_calls(snap):
    """The session counts attempted gestures; everything else must stay put."""
    return {**snap, "tool_calls": None}


def protected(controller, identity):
    """Bytes of every object that enters a hash or a signature for one action."""
    items = {}
    draft = controller._drafts.get(identity)
    if draft and "display" in draft:
        items["draft_card"] = H(draft["display"].card)
    pending = controller._pending.get(identity)
    if pending and pending.get("mode") == "prepared":
        prepared = pending["prepared"]
        items["action"] = H(prepared.action)
        items["cert"] = H(prepared.cert)
        items["card"] = H(prepared.proposal.card)
        items["display_card"] = H(pending["display"].card)
        items["challenge"] = pending.get("challenge_id")
        items["verified"] = pending.get("verified")
    items["state"] = H(controller.state)
    items["audit_head"] = controller.gateway.audit.checkpoint()["head"]
    return items


SPIED = (
    ("gateway", "request_mfa"),
    ("gateway", "submit_mfa"),
    ("gateway", "dispatch"),
    ("gateway", "device_control"),
    ("gateway", "resume"),
    ("gateway.mfa", "verify"),
    ("device", "approve"),
    ("device", "complete_mfa"),
    ("workflow", "confirm_transfer"),
    ("workflow", "confirm_prepared"),
    ("workflow", "confirm_plan"),
)


class Spy:
    """Counts of every call that issues or verifies a challenge, consumes an approval,
    signs on the device, dispatches to the bank or resumes a plan."""

    def __init__(self, controller, monkeypatch):
        self.counts = Counter()
        for path, name in SPIED:
            target = controller
            for part in path.split("."):
                target = getattr(target, part)
            original = getattr(target, name)

            def wrapper(*args, _name=name, _original=original, **kwargs):
                self.counts[_name] += 1
                return _original(*args, **kwargs)

            monkeypatch.setattr(target, name, wrapper)

    def total(self):
        return sum(self.counts.values())

    def reset(self):
        self.counts.clear()


def start_chat(monkeypatch, text=FRAUD, *, locale="zh-CN"):
    monkeypatch.setenv("CORRIDOR_LOCALE", locale)
    page = app()
    page.button(key="enter").click().run()
    page.text_input(key="domestic_text").set_value(text)
    page.button(key="domestic_send").click().run()
    assert not page.exception
    controller = page.session_state["controller"]
    session = page.session_state["domestic_session"]
    identifier = page.session_state["domestic_response_id"]
    return page, controller, session, identifier


def outcome(page, controller, session, identifier, tier, card_hash):
    final = session.response(identifier)
    return (
        final["status"],
        tier,
        card_hash,
        tuple(final["payload"].get("rule_ids", [])),
        tuple(row["event"] for row in controller.gateway.audit.records()),
        controller.gateway.audit.checkpoint()["head"],
        controller.gateway.bank.execute_count,
        controller.gateway.safety_snapshot(controller.device.principal)["state"],
    )


def complete_red_transfer(page, controller, session, identifier):
    page.button(key="domestic_confirm").click().run()
    page.button(key="domestic_verify_mfa:request").click().run()
    pending_id = session.response(identifier)["payload"]["pending_id"]
    code = controller.gateway.mfa.test_code(controller._pending[pending_id]["challenge_id"])
    page.text_input(key="domestic_verify_mfa:code").set_value(code)
    page.button(key="domestic_verify_mfa:verify").click().run()
    page.button(key="domestic_confirm").click().run()
    assert not page.exception


def _run_transfer(monkeypatch, *, gate):
    """gate: "off" (level patched to 0), "proceed" (safe answers) or "pause"."""
    if gate == "off":
        monkeypatch.setattr(scam_friction, "level", lambda *args, **kwargs: 0)
    page, controller, session, identifier = start_chat(monkeypatch)
    first = session.response(identifier)
    card_hash, tier = H(first["card"]), first["tier"]
    at_card = snapshot(controller, session)
    gated = bool(gate_keys(page, "domestic", identifier))
    if gate == "off":
        assert not gated and "domestic_confirm" in button_keys(page)
        assert controller.scam_gate_status(first["payload"]["draft_id"]) is None
    else:
        assert gated and "domestic_confirm" not in button_keys(page)
        assert "domestic_verify_mfa:request" not in button_keys(page)
        assert "domestic_cancel" in button_keys(page)
        answer_gate(page, "domestic", identifier, answers=SAFE if gate == "proceed" else GUIDED)
    if gate == "pause":
        return page, controller, session, identifier, at_card, (tier, card_hash)
    complete_red_transfer(page, controller, session, identifier)
    return page, controller, session, identifier, at_card, (tier, card_hash)


def _pause_pending(monkeypatch, text=NEW, *, challenge=False, verified=False):
    """A pending red transfer paused on the chat page, optionally with a challenge or a
    completed verification before the pause."""
    page, controller, session, identifier = start_chat(monkeypatch, text)
    answer_gate(page, "domestic", identifier, answers=SAFE)
    page.button(key="domestic_confirm").click().run()
    assert not page.exception
    pending_id = session.response(identifier)["payload"]["pending_id"]
    if challenge or verified:
        page.button(key="domestic_verify_mfa:request").click().run()
        assert not page.exception
        assert controller._pending[pending_id]["challenge_id"]
    if verified:
        code = controller.gateway.mfa.test_code(controller._pending[pending_id]["challenge_id"])
        page.text_input(key="domestic_verify_mfa:code").set_value(code)
        page.button(key="domestic_verify_mfa:verify").click().run()
        assert not page.exception
        assert controller._pending[pending_id]["verified"]
    answer_gate(page, "domestic", identifier, answers=GUIDED)
    assert _gate(controller, pending_id)[0] == "pause"
    return page, controller, session, identifier, pending_id


def _entry_calls(controller, session, identifier, identity, code="000000"):
    """Every entry point that could request MFA, verify, confirm or dispatch an action.

    A draft that was never confirmed has no pending object yet, so only the first
    confirmation (controller, session, IM adapter) applies to it; the verification
    gestures then report that the intent must be confirmed first, which is the
    pre-S6 behaviour and issues nothing. A pending previewed by the controller itself
    (a scheduled transfer, a redisplay replacement) was never a chat draft, so the
    draft confirmation does not apply to it.
    """
    pending = identity in controller._pending
    calls = {}
    if identity in controller._drafts:
        calls["confirm_transfer"] = lambda: controller.confirm_transfer(identity)
    if session._modes.get(identifier) in {"transfer", "pending"}:
        # A clarification reply has no session gesture of its own: the page confirms
        # its local selection through the controller directly.
        calls.update({
            "session.confirm": lambda: session.confirm(identifier),
            "im.confirm": lambda: IMAdapter(session).click("confirm", identifier),
            "session.verify_mfa": lambda: session.verify_mfa(identifier),
            "im.verify_mfa": lambda: IMAdapter(session).click("verify_mfa", identifier),
        })
    if pending:
        calls.update({
            "request_mfa": lambda: controller.request_mfa(identity),
            "complete_mfa": lambda: controller.complete_mfa(identity, code=code),
            "approve_pending": lambda: controller.approve_pending(identity),
        })
    return calls


def _reason_of(result):
    return result.get("reason") or (result.get("payload") or {}).get("reason")


def assert_all_refused(controller, session, identifier, identity, *, reason, spy, code="000000"):
    """Call every entry point: each is refused for ``reason`` and no side effect runs."""
    before = snapshot(controller, session)
    guarded = protected(controller, identity)
    spy.reset()
    pending = identity in controller._pending
    for name, call in _entry_calls(controller, session, identifier, identity, code).items():
        result = call()
        if not pending and name.endswith("verify_mfa"):
            assert result["status"] == "intent_confirmation_required", (name, result)
            continue
        assert result["status"] == "paused", (name, result)
        assert _reason_of(result) == reason, (name, result)
        assert result.get("rule_ids", []) == [] or result.get("payload", {}).get("rule_ids") == []
    assert spy.total() == 0, dict(spy.counts)
    assert _without_calls(snapshot(controller, session)) == _without_calls(before)
    assert protected(controller, identity) == guarded


# --- three runs: gate off, gate on and answered safe, gate on and paused ---------------


def test_three_runs_off_proceed_and_pause(monkeypatch):
    with monkeypatch.context() as patched:
        page, controller, session, identifier, _, (tier, card_hash) = _run_transfer(
            patched, gate="off"
        )
        off = outcome(page, controller, session, identifier, tier, card_hash)
    page, controller, session, identifier, _, (tier, card_hash) = _run_transfer(
        monkeypatch, gate="proceed"
    )
    on = outcome(page, controller, session, identifier, tier, card_hash)
    assert on == off
    assert off[0] == "settled" and off[1] == "red" and off[6] == 1
    assert "approval_requested" in off[4] and "dispatch_intent" in off[4]
    page, controller, session, identifier, at_card, (tier, card_hash) = _run_transfer(
        monkeypatch, gate="pause"
    )
    keys = button_keys(page)
    assert "domestic_confirm" not in keys and "domestic_verify_mfa:request" not in keys
    assert "domestic_cancel" in keys
    assert any(key.endswith(":retry") for key in keys)
    paused = snapshot(controller, session)
    assert paused == at_card
    assert paused["audit_events"] == ("login", "user_utterance")
    assert paused["budget"]["used"] == 0 and paused["execute_count"] == 0
    assert paused["pending"] == () and paused["challenges"] == ()
    assert session.response(identifier)["status"] == "awaiting_input"
    assert session.response(identifier)["tier"] == "red"
    assert H(session.response(identifier)["card"]) == card_hash
    page.button(key="domestic_cancel").click().run()
    assert not page.exception
    assert session.response(identifier)["status"] == "withdrawn"
    assert controller.gateway.bank.execute_count == 0


def test_state_diff_before_and_after_the_gate_is_empty_until_the_user_proceeds(monkeypatch):
    page, controller, session, identifier = start_chat(monkeypatch)
    before = snapshot(controller, session)
    draft_id = session.response(identifier)["payload"]["draft_id"]
    guarded = protected(controller, draft_id)
    for answers in (
        {"verified": "yes"},
        {"verified": "yes", "guided": "yes"},
    ):
        answer_gate(page, "domestic", identifier, answers=answers)
        assert snapshot(controller, session) == before
        assert protected(controller, draft_id) == guarded
        assert "domestic_confirm" not in button_keys(page)
    retry_gate(page)
    for answers in (UNVERIFIED, {"verified": "unsure", "guided": "yes"}):
        page.run()
        answer_gate(page, "domestic", identifier, answers=answers)
        assert snapshot(controller, session) == before
        assert "domestic_confirm" not in button_keys(page)
        retry_gate(page)
    answer_gate(page, "domestic", identifier, answers=SAFE)
    assert snapshot(controller, session) == before
    assert protected(controller, draft_id) == guarded
    assert "domestic_confirm" in button_keys(page)
    status = controller.scam_gate_status(draft_id)
    assert status["decision"] == "proceed" and status["answers"] == SAFE
    assert status["required"] and status["epoch"] == 3 and status["retries"] == 3
    # The answers are not in the session envelope, the card or any proposal.
    envelope = json.dumps(session.response(identifier), ensure_ascii=False)
    assert "scam" not in envelope and "guided" not in envelope and "verified" not in envelope


def test_pause_after_an_existing_challenge_refuses_every_entry_then_resumes(monkeypatch):
    with monkeypatch.context() as patched:
        page, controller, session, identifier, _, (tier, card_hash) = _run_transfer(
            patched, gate="off"
        )
        off = outcome(page, controller, session, identifier, tier, card_hash)
    page, controller, session, identifier = start_chat(monkeypatch)
    first = session.response(identifier)
    card_hash, tier = H(first["card"]), first["tier"]
    answer_gate(page, "domestic", identifier, answers=SAFE)
    page.button(key="domestic_confirm").click().run()
    page.button(key="domestic_verify_mfa:request").click().run()
    assert not page.exception
    pending_id = session.response(identifier)["payload"]["pending_id"]
    challenge = controller._pending[pending_id]["challenge_id"]
    assert challenge is not None
    code = controller.gateway.mfa.test_code(challenge)
    spy = Spy(controller, monkeypatch)
    # The user changes one answer after the challenge exists: the page shows the
    # challenge note and nothing that could continue.
    answer_gate(page, "domestic", identifier, answers=GUIDED)
    keys = button_keys(page)
    assert "domestic_verify_mfa:verify" not in keys and "domestic_confirm" not in keys
    assert "domestic_verify_mfa:request" not in keys and "domestic_cancel" in keys
    assert not [item for item in page.text_input if item.key == "domestic_verify_mfa:code"]
    assert UI_MESSAGES["polish.scam.pause_challenge"]["zh-CN"] in shown_text(page)
    assert gate_disabled(page, "domestic", identifier) == {"verified": True, "guided": True}
    assert_all_refused(
        controller, session, identifier, pending_id, reason="scam_pause", spy=spy, code=code
    )
    assert controller._pending[pending_id]["challenge_id"] == challenge
    assert not controller._pending[pending_id]["verified"]
    assert controller.gateway.mfa._challenges[challenge].attempts == 0
    # Resume: "I have confirmed this on my own (self-reported)" only clears the answers.
    retry_gate(page)
    assert _gate(controller, pending_id) == ("incomplete", BLANK, 2, 1)
    assert gate_values(page, "domestic", identifier) == BLANK
    assert gate_disabled(page, "domestic", identifier) == {"verified": False, "guided": False}
    assert "domestic_verify_mfa:verify" not in button_keys(page)
    assert spy.total() == 0
    answer_gate(page, "domestic", identifier, answers=SAFE)
    page.text_input(key="domestic_verify_mfa:code").set_value(code)
    page.button(key="domestic_verify_mfa:verify").click().run()
    assert controller._pending[pending_id]["verified"]
    page.button(key="domestic_confirm").click().run()
    assert not page.exception
    assert outcome(page, controller, session, identifier, tier, card_hash) == off
    assert spy.counts["verify"] == 1 and spy.counts["dispatch"] == 1
    assert spy.counts["approve"] == 1 and spy.counts["request_mfa"] == 0


def test_repeated_reruns_keep_the_pause_and_change_nothing(monkeypatch):
    page, controller, session, identifier, at_card, _ = _run_transfer(monkeypatch, gate="pause")
    draft_id = session.response(identifier)["payload"]["draft_id"]
    for _ in range(3):
        page.run()
        assert not page.exception
        keys = button_keys(page)
        assert "domestic_confirm" not in keys and "domestic_verify_mfa:request" not in keys
        assert _gate(controller, draft_id) == ("pause", GUIDED, 2, 0)
        assert snapshot(controller, session) == at_card
    text = shown_text(page)
    assert UI_MESSAGES["polish.scam.trigger.guided_yes"]["zh-CN"] in text
    assert "挂断电话" in text
    assert controller.scam_gate_rejections() == []


def test_stale_button_events_are_refused_by_the_controller(monkeypatch):
    page, controller, session, identifier = start_chat(monkeypatch)
    answer_gate(page, "domestic", identifier, answers=SAFE)
    assert "domestic_confirm" in button_keys(page)
    before = snapshot(controller, session)
    draft_id = session.response(identifier)["payload"]["draft_id"]
    spy = Spy(controller, monkeypatch)
    # A stale page sends the confirm click together with the changed answer: the confirm
    # callback syncs the gate from the widgets first, so the click meets a paused gate.
    keys = gate_keys(page, "domestic", identifier)
    page.radio(key=keys["guided"]).set_value("yes")
    page.button(key="domestic_confirm").click().run()
    assert not page.exception
    assert spy.total() == 0
    # The session counted the attempted gesture; nothing else moved.
    assert snapshot(controller, session) == {**before, "tool_calls": before["tool_calls"] + 1}
    assert session.response(identifier)["status"] == "paused"
    assert UI_MESSAGES["polish.scam.paused_copy"]["zh-CN"] in shown_text(page)
    assert "domestic_confirm" not in button_keys(page)
    # Gestures replayed outside the page (an old tab, another channel) meet the same gate.
    assert_all_refused(controller, session, identifier, draft_id, reason="scam_pause", spy=spy)
    assert draft_id in controller._drafts and not controller._pending
    # After answering again the same draft continues through the ordinary path.
    retry_gate(page)
    answer_gate(page, "domestic", identifier, answers=SAFE)
    page.button(key="domestic_confirm").click().run()
    assert not page.exception
    assert session.response(identifier)["status"] == "waiting_mfa"
    assert session.response(identifier)["payload"]["pending_id"] == draft_id
    assert spy.counts["confirm_transfer"] == 1 and spy.counts["dispatch"] == 0


def test_answers_are_bound_to_the_action_instance_and_its_card(monkeypatch):
    page, controller, session, identifier = start_chat(monkeypatch)
    answer_gate(page, "domestic", identifier, answers=SAFE)
    first_draft = session.response(identifier)["payload"]["draft_id"]
    assert "domestic_confirm" in button_keys(page)
    # A new request (another amount to the same new payee) gets fresh, unanswered questions.
    send_message(page, "工作人员说立即给新联系人转300元，不然账户冻结")
    second = page.session_state["domestic_response_id"]
    assert second != identifier
    assert gate_values(page, "domestic", second) == BLANK
    assert "domestic_confirm" not in button_keys(page)
    second_draft = session.response(second)["payload"]["draft_id"]
    assert _gate(controller, second_draft)[0] == "incomplete"
    assert _gate(controller, first_draft)[0] == "proceed"
    # The controller computes the card identity itself from its authoritative card.
    status = controller.scam_gate_status(second_draft)
    card = session.response(second)["card"]
    assert status["card_identity"] == scam_friction.card_identity(card)
    assert status["current_card_identity"] == status["card_identity"] and not status["stale"]
    assert status["level"] == 2 and status["cues"] == ("authority", "urgency")
    # Widget keys carry the action instance, the full card identity and the epoch.
    for key in gate_keys(page, "domestic", second).values():
        assert key.startswith(f"domestic_scam:{second}:{second_draft}:{status['card_identity']}:0:")
    # A caller cannot bind answers to another card: the event is refused and recorded.
    controller.scam_gate_answer(second_draft, card_identity="not-the-card", epoch=0, answers=GUIDED)
    assert _gate(controller, second_draft) == ("incomplete", BLANK, 2, 0)
    assert controller.scam_gate_rejections()[-1]["reason"] == "card_mismatch"
    assert controller._scam_refusal("unknown-action") is None
    assert controller.scam_gate_status("unknown-action") is None


@pytest.mark.parametrize(
    "answers",
    [
        None,
        {"verified": "yes"},
        SAFE,
        UNSURE,
        UNVERIFIED,
        GUIDED,
        {"verified": "no", "guided": "yes"},
    ],
)
def test_answer_mutations_leave_every_protected_object_identical(answers, monkeypatch):
    controller, session = _fresh()
    first = session.receive(NEW)
    draft_id = first["payload"]["draft_id"]
    card = first["card"]
    base_snapshot = snapshot(controller, session)
    base_protected = protected(controller, draft_id)
    spy = Spy(controller, monkeypatch)
    status = controller.scam_gate_establish(draft_id, text=None)
    assert status["level"] == 1 and status["card_identity"] == scam_friction.card_identity(card)
    status = controller.scam_gate_answer(
        draft_id, card_identity=status["card_identity"], epoch=0, answers=answers
    )
    assert status["decision"] == scam_friction.decision(1, answers)
    assert snapshot(controller, session) == base_snapshot
    assert protected(controller, draft_id) == base_protected
    assert controller.state.now == base_snapshot["clock"]
    assert session.response(first["response_id"]) == first
    assert spy.total() == 0
    # Only the gate status and whether the ordinary path continues differ: the entry
    # points fail closed on an unanswered check exactly as on a pause.
    result = controller.confirm_transfer(draft_id)
    if status["decision"] != "proceed":
        assert result["status"] == "paused" and result["draft_id"] == draft_id
        expected = "scam_pause" if status["decision"] == "pause" else "scam_incomplete"
        assert result["reason"] == expected
        assert H(result["card"]) == H(card)
        assert snapshot(controller, session) == base_snapshot
        assert spy.total() == 0
        # The returned structures are copies: changing them touches nothing inside.
        result["card"]["amount"] = "1/1"
        result["contacts"].append("CONTACT_CHEN")
        assert H(controller._drafts[draft_id]["display"].card) == H(card)
        assert "CONTACT_CHEN" not in controller._drafts[draft_id]["candidate"].contact_refs
    else:
        assert result["status"] == "waiting_mfa" and result["pending_id"] == draft_id
        assert controller.gateway.approval_budget(controller.device.principal)["used"] == 1
        assert spy.counts["confirm_transfer"] == 1 and spy.counts["dispatch"] == 0


def test_gate_is_visually_separate_and_judge_record_stays_out_of_the_customer_view(monkeypatch):
    page, controller, session, identifier = start_chat(monkeypatch)
    text = shown_text(page)
    assert UI_MESSAGES["polish.scam.title"]["zh-CN"] in text
    assert UI_MESSAGES["polish.scam.not_signed"]["zh-CN"] in text
    assert UI_MESSAGES["polish.scam.fact_new"]["zh-CN"] in text
    assert UI_MESSAGES["polish.scam.fact_source"]["zh-CN"] in text
    assert UI_MESSAGES["polish.scam.footer"]["zh-CN"] in text
    assert UI_MESSAGES["polish.scam.incomplete"]["zh-CN"] in text
    assert UI_MESSAGES["polish.scam.cue.authority"]["zh-CN"] in text
    assert "防骗提醒" in text
    assert 'class="scam-title"' in text and 'class="approval-card"' in text
    # The default view is the judge view: the interaction record is shown there only.
    assert "交互记录（展示层，不进审计链）" in text
    assert "门控状态＝未答完" in text
    monkeypatch.setenv("CORRIDOR_DEFAULT_VIEW", "customer")
    page, controller, session, identifier = start_chat(monkeypatch)
    text = shown_text(page)
    # R6: the customer's titles say "页面提示" / "a page prompt", never 展示层 / display-layer
    # (the judge's titles are unchanged).
    assert UI_MESSAGES["customer:polish.scam.title"]["zh-CN"] in text
    assert UI_MESSAGES["polish.scam.title"]["zh-CN"] not in text
    assert UI_MESSAGES["customer:polish.scam.facts_title"]["zh-CN"] in text
    assert UI_MESSAGES["polish.scam.facts_title"]["zh-CN"] not in text
    assert "交互记录（展示层，不进审计链）" not in text
    assert UI_MESSAGES["polish.scam.judge_note"]["zh-CN"] not in text
    # R5: the customer reads a plain line instead of the judge's isolation paragraph,
    # and never the word 门控 or the kernel's vocabulary (the English says "this check").
    assert UI_MESSAGES["customer:polish.scam.not_signed"]["zh-CN"] in text
    assert UI_MESSAGES["polish.scam.not_signed"]["zh-CN"] not in text
    # (冻结内核 stays: the gate's footer must carry the fixed sentence "它不修改冻结
    # 内核的规则与签名卡" word for word.)
    for jargon in ("门控", "规范卡", "签名载荷", "审计链", "前置条件", "展示层"):
        assert jargon not in text, jargon
    # The customer sees the gate's own footer next to the fraud warning's footer: the
    # two are adjacent and distinguishable (the warning speaks of itself, the gate of
    # the pause it may cause).
    assert UI_MESSAGES["polish.fraud.footer"]["zh-CN"] in text
    assert UI_MESSAGES["polish.scam.footer"]["zh-CN"] in text
    order = [
        marker
        for item in page.markdown
        for marker in ("approval-card", "fraud-footer", "scam-footer")
        if marker in str(item.value)
    ]
    assert order.index("fraud-footer") < order.index("scam-footer")
    answer_gate(page, "domestic", identifier, answers=UNVERIFIED)
    text = shown_text(page)
    assert UI_MESSAGES["polish.scam.pause_self_report"]["zh-CN"] in text
    assert UI_MESSAGES["polish.scam.trigger.verified_no"]["zh-CN"] in text
    assert UI_MESSAGES["polish.scam.pause_title"]["zh-CN"] in text
    assert "挂断电话" in text
    assert "门控" not in text
    labels = {button.label for button in page.button}
    assert UI_MESSAGES["polish.scam.pause_retry"]["zh-CN"] in labels
    assert UI_MESSAGES["polish.scam.pause_retry_guided"]["zh-CN"] not in labels


def test_drill_shortcut_sends_the_scripted_scam_message_and_shows_the_phrase_gate(
    monkeypatch,
):
    # R7: the drill now shows the phrase gate's fixed reply: no card, no questions, no
    # confirmation, nothing counted against the approval budget.
    monkeypatch.setenv("CORRIDOR_LOCALE", "zh-CN")
    page = app()
    page.button(key="enter").click().run()
    page.button(key="domestic_example_scam_drill").click().run()
    assert not page.exception
    controller = page.session_state["controller"]
    session = page.session_state["domestic_session"]
    identifier = page.session_state["domestic_response_id"]
    response = session.response(identifier)
    assert not response["card"] and response["payload"]["scam_phrase"]
    assert not gate_keys(page, "domestic", identifier)
    assert "domestic_confirm" not in button_keys(page)
    shown = shown_text(page) + "\n".join(str(item.value) for item in page.markdown)
    assert "96110" in shown
    assert controller.gateway.bank.execute_count == 0
    assert controller.gateway.approval_budget(controller.device.principal)["used"] == 0
    # The same click twice gives the same reply.
    again = app()
    again.button(key="enter").click().run()
    again.button(key="domestic_example_scam_drill").click().run()
    twin = again.session_state["domestic_session"].response(
        again.session_state["domestic_response_id"]
    )
    assert twin["payload"]["scam_phrase"] == response["payload"]["scam_phrase"]


def _choose(page, contact):
    page.selectbox(key="domestic_clarify_contact").select(contact).run()
    page.button(key="domestic_select_contact").click().run()
    assert not page.exception


def test_clarification_card_path_is_gated_the_same_way(monkeypatch):
    page, controller, session, identifier = start_chat(monkeypatch, CLARIFY)
    response = session.response(identifier)
    assert response["status"] == "clarify" and response["payload"]["contacts"]
    _choose(page, "CONTACT_LIN_B")
    local = page.session_state[f"domestic_selected:{identifier}"]
    assert local["card"]["objects"]["contact_ref"] == "CONTACT_LIN_B"
    before = snapshot(controller, session)
    assert set(gate_keys(page, "domestic", identifier)) == {"verified", "guided"}
    assert "domestic_selected_confirm" not in button_keys(page)
    assert "domestic_selected_cancel" in button_keys(page)
    spy = Spy(controller, monkeypatch)
    answer_gate(page, "domestic", identifier, answers=GUIDED)
    assert "domestic_selected_confirm" not in button_keys(page)
    assert controller.confirm_transfer(local["draft_id"])["reason"] == "scam_pause"
    assert snapshot(controller, session) == before and spy.total() == 0
    retry_gate(page)
    answer_gate(page, "domestic", identifier, answers=SAFE)
    assert "domestic_selected_confirm" in button_keys(page)
    page.button(key="domestic_selected_confirm").click().run()
    assert not page.exception
    assert page.session_state[f"domestic_selected:{identifier}"]["status"] == "settled"
    assert controller.gateway.bank.execute_count == 1
    assert spy.counts["dispatch"] == 1 and spy.counts["approve"] == 1


def test_device_page_shows_facts_only_and_honours_a_chat_pause(monkeypatch):
    page, controller, session, identifier = start_chat(monkeypatch, NEW)
    answer_gate(page, "domestic", identifier, answers=SAFE)
    page.button(key="domestic_confirm").click().run()
    pending_id = session.response(identifier)["payload"]["pending_id"]
    navigate(page, "device")
    assert not page.exception
    text = shown_text(page)
    assert UI_MESSAGES["polish.scam.device_title"]["zh-CN"] in text
    assert UI_MESSAGES["polish.scam.fact_new"]["zh-CN"] in text
    assert not page.radio
    assert f"device_mfa:{pending_id}:request" in button_keys(page)
    assert f"approve:{pending_id}" in button_keys(page)
    # R6: the customer's device page says "页面提示" where the judge's says 展示层.
    page.button(key="view_toggle").click().run()
    assert not page.exception and page.session_state["polish_view"] == "customer"
    customer_text = shown_text(page)
    assert UI_MESSAGES["customer:polish.scam.device_title"]["zh-CN"] in customer_text
    assert UI_MESSAGES["polish.scam.device_title"]["zh-CN"] not in customer_text
    assert "展示层" not in customer_text
    page.button(key="view_toggle").click().run()
    assert not page.exception and page.session_state["polish_view"] == "judge"
    # A pause answered on the chat page holds on the device page too.
    navigate(page, "conversation")
    answer_gate(page, "domestic", identifier, answers=GUIDED)
    spy = Spy(controller, monkeypatch)
    before = snapshot(controller, session)
    navigate(page, "device")
    assert not page.exception
    text = shown_text(page)
    assert UI_MESSAGES["polish.scam.device_paused"]["zh-CN"] in text
    assert UI_MESSAGES["polish.scam.pause_challenge"]["zh-CN"] not in text
    assert f"device_mfa:{pending_id}:request" not in button_keys(page)
    assert f"approve:{pending_id}" not in button_keys(page)
    assert f"cancel:{pending_id}" in button_keys(page)
    assert f"scam_back:{pending_id}" in button_keys(page)
    assert controller.request_mfa(pending_id)["status"] == "paused"
    assert snapshot(controller, session) == before and spy.total() == 0
    # With a challenge issued before the pause the device page says its window runs on.
    navigate(page, "conversation")
    retry_gate(page)
    answer_gate(page, "domestic", identifier, answers=SAFE)
    page.button(key="domestic_verify_mfa:request").click().run()
    assert controller._pending[pending_id]["challenge_id"]
    answer_gate(page, "domestic", identifier, answers=GUIDED)
    navigate(page, "device")
    text = shown_text(page)
    assert UI_MESSAGES["polish.scam.device_paused"]["zh-CN"] in text
    assert UI_MESSAGES["polish.scam.pause_challenge"]["zh-CN"] in text
    assert f"device_mfa:{pending_id}:request" not in button_keys(page)


# --- the second review round: the state machine at every entry ---------------------------


def test_entry_points_refuse_after_a_retry_before_any_rerender(monkeypatch):
    """E1: the retry gesture (direct, before the page redraws) leaves an incomplete gate,
    never an absent one: every entry point still refuses, with spies at zero."""
    page, controller, session, identifier, pending_id = _pause_pending(monkeypatch, challenge=True)
    code = controller.gateway.mfa.test_code(controller._pending[pending_id]["challenge_id"])
    status = controller.scam_gate_status(pending_id)
    spy = Spy(controller, monkeypatch)
    assert_all_refused(
        controller, session, identifier, pending_id, reason="scam_pause", spy=spy, code=code
    )
    retried = controller.scam_gate_retry(
        pending_id, card_identity=status["card_identity"], epoch=status["epoch"]
    )
    assert retried["decision"] == "incomplete" and retried["epoch"] == 1 and retried["required"]
    assert controller.scam_gate_status(pending_id) is not None
    assert_all_refused(
        controller, session, identifier, pending_id, reason="scam_incomplete", spy=spy, code=code
    )
    # Only a complete answer for the new epoch opens the path again.
    controller.scam_gate_answer(
        pending_id, card_identity=status["card_identity"], epoch=1, answers=SAFE
    )
    assert controller.complete_mfa(pending_id, code=code)["status"] == "mfa_verified"
    assert spy.counts["verify"] == 1 and spy.counts["dispatch"] == 0


def test_card_change_while_paused_requires_new_answers_and_rejects_late_callbacks(monkeypatch):
    """E2: a changed card (the clarification card's other contact) re-grades the gate and
    invalidates the answers; a late callback for the old card is refused; a direct call
    that switches the contact meets a stale gate (second-round finding R4b)."""
    page, controller, session, identifier = start_chat(monkeypatch, CLARIFY)
    _choose(page, "CONTACT_LIN_B")
    draft_id = page.session_state[f"domestic_selected:{identifier}"]["draft_id"]
    answer_gate(page, "domestic", identifier, answers=GUIDED)
    bound = controller.scam_gate_status(draft_id)
    assert bound["decision"] == "pause" and bound["epoch"] == 0 and bound["level"] == 1
    card_b = bound["card_identity"]
    spy = Spy(controller, monkeypatch)
    before = snapshot(controller, session)
    # (a) The direct call switches the displayed contact in the same call: the gate is
    # bound to LIN_B, the current card is LIN_A -> stale, nothing runs.
    result = controller.confirm_transfer(draft_id, contact_ref="CONTACT_LIN_A")
    assert result["status"] == "paused" and result["reason"] == "scam_stale"
    assert spy.total() == 0 and not controller._pending
    assert controller.gateway.bank.execute_count == 0
    assert controller.scam_gate_status(draft_id)["stale"]
    assert controller.confirm_transfer(draft_id)["reason"] == "scam_stale"
    assert _without_calls(snapshot(controller, session)) == _without_calls(before)
    # (b) The page redraws for LIN_A: an explicit re-grade, blank answers, new epoch.
    _choose(page, "CONTACT_LIN_A")
    rebound = controller.scam_gate_status(draft_id)
    assert rebound["card_identity"] != card_b and not rebound["stale"]
    assert rebound["decision"] == "incomplete" and rebound["epoch"] == 1
    assert rebound["answers"] == BLANK and rebound["regrades"] == 1
    assert gate_values(page, "domestic", identifier) == BLANK
    assert "domestic_selected_confirm" not in button_keys(page)
    assert controller.confirm_transfer(draft_id)["reason"] == "scam_incomplete"
    # (c) A late callback for LIN_B (old card, old epoch) changes nothing.
    controller.scam_gate_answer(draft_id, card_identity=card_b, epoch=0, answers=SAFE)
    controller.scam_gate_answer(draft_id, card_identity=card_b, epoch=1, answers=SAFE)
    controller.scam_gate_retry(draft_id, card_identity=card_b, epoch=0)
    assert controller.scam_gate_status(draft_id)["answers"] == BLANK
    assert [item["reason"] for item in controller.scam_gate_rejections()[-3:]] == [
        "card_mismatch",
        "card_mismatch",
        "card_mismatch",
    ]
    assert controller.confirm_transfer(draft_id)["reason"] == "scam_incomplete"
    assert spy.total() == 0
    # (d) Back to LIN_B: the old answers are not revived; the user answers again.
    _choose(page, "CONTACT_LIN_B")
    assert controller.scam_gate_status(draft_id)["card_identity"] == card_b
    assert controller.scam_gate_status(draft_id)["epoch"] == 2
    assert gate_values(page, "domestic", identifier) == BLANK
    assert controller.confirm_transfer(draft_id)["reason"] == "scam_incomplete"
    answer_gate(page, "domestic", identifier, answers=SAFE)
    assert "domestic_selected_confirm" in button_keys(page)
    page.button(key="domestic_selected_confirm").click().run()
    assert not page.exception
    assert page.session_state[f"domestic_selected:{identifier}"]["status"] == "settled"
    assert spy.counts["confirm_transfer"] == 1 and spy.counts["dispatch"] == 1


def test_same_batch_answer_change_and_gesture_never_outruns_the_gate(monkeypatch):
    """E3: one Streamlit run carrying both an answer change and a gesture, at every
    stage of the red path; stale-epoch events and the old retry button are refused."""
    page, controller, session, identifier = start_chat(monkeypatch, FRAUD)
    draft_id = session.response(identifier)["payload"]["draft_id"]
    answer_gate(page, "domestic", identifier, answers=SAFE)
    assert "domestic_confirm" in button_keys(page)
    spy = Spy(controller, monkeypatch)
    before = snapshot(controller, session)
    keys = gate_keys(page, "domestic", identifier)
    # (a) proceed -> flip verified=no (level 2 pause) + confirm in one run.
    page.radio(key=keys["verified"]).set_value("no")
    page.button(key="domestic_confirm").click().run()
    assert not page.exception
    assert session.response(identifier)["status"] == "paused"
    assert not controller._pending and spy.total() == 0
    assert _without_calls(snapshot(controller, session)) == _without_calls(before)
    # (b) pause -> flip verified=yes + retry in one run: the pause is latched, so the
    # flip is refused; the retry opens epoch 1 with blank answers.
    retry = next(key for key in button_keys(page) if key.endswith(":retry"))
    page.radio(key=keys["verified"]).set_value("yes")
    page.button(key=retry).click().run()
    assert not page.exception
    assert _gate(controller, draft_id) == ("incomplete", BLANK, 2, 1)
    assert "latched" in [item["reason"] for item in controller.scam_gate_rejections()]
    assert "domestic_confirm" not in button_keys(page)
    assert gate_values(page, "domestic", identifier) == BLANK
    # Stale epoch events and the old retry button change nothing.
    card_identity = controller.scam_gate_status(draft_id)["card_identity"]
    controller.scam_gate_answer(draft_id, card_identity=card_identity, epoch=0, answers=SAFE)
    controller.scam_gate_retry(draft_id, card_identity=card_identity, epoch=0)
    assert _gate(controller, draft_id) == ("incomplete", BLANK, 2, 1)
    assert [item["reason"] for item in controller.scam_gate_rejections()[-2:]] == [
        "epoch_mismatch",
        "epoch_mismatch",
    ]
    # (c) confirm -> waiting_mfa; flip guided=yes + request code in one run: no challenge.
    answer_gate(page, "domestic", identifier, answers=SAFE)
    page.button(key="domestic_confirm").click().run()
    pending_id = session.response(identifier)["payload"]["pending_id"]
    assert spy.counts["confirm_transfer"] == 1
    assert "domestic_verify_mfa:request" in button_keys(page)
    keys = gate_keys(page, "domestic", identifier)
    page.radio(key=keys["guided"]).set_value("yes")
    page.button(key="domestic_verify_mfa:request").click().run()
    assert not page.exception
    assert controller._pending[pending_id]["challenge_id"] is None
    assert spy.counts["request_mfa"] == 0
    assert _gate(controller, pending_id)[0] == "pause"
    # (d) answer again safe, request a code, then flip guided=yes + submit the right code
    # in one run: not verified, attempts not counted.
    retry_gate(page)
    answer_gate(page, "domestic", identifier, answers=SAFE)
    page.button(key="domestic_verify_mfa:request").click().run()
    challenge = controller._pending[pending_id]["challenge_id"]
    assert challenge and spy.counts["request_mfa"] == 1
    code = controller.gateway.mfa.test_code(challenge)
    keys = gate_keys(page, "domestic", identifier)
    page.radio(key=keys["guided"]).set_value("yes")
    page.text_input(key="domestic_verify_mfa:code").set_value(code)
    page.button(key="domestic_verify_mfa:verify").click().run()
    assert not page.exception
    assert not controller._pending[pending_id]["verified"]
    assert controller.gateway.mfa._challenges[challenge].attempts == 0
    assert spy.counts["verify"] == 0 and spy.counts["submit_mfa"] == 0
    assert "mfa_attempt" not in [row["event"] for row in controller.gateway.audit.records()]
    # (e) verified -> flip guided=yes + final confirm in one run: no dispatch.
    retry_gate(page)
    answer_gate(page, "domestic", identifier, answers=SAFE)
    page.text_input(key="domestic_verify_mfa:code").set_value(code)
    page.button(key="domestic_verify_mfa:verify").click().run()
    assert controller._pending[pending_id]["verified"] and spy.counts["verify"] == 1
    assert "domestic_confirm" in button_keys(page)
    keys = gate_keys(page, "domestic", identifier)
    page.radio(key=keys["guided"]).set_value("yes")
    page.button(key="domestic_confirm").click().run()
    assert not page.exception
    assert controller.gateway.bank.execute_count == 0
    assert spy.counts["dispatch"] == 0 and spy.counts["approve"] == 0
    assert spy.counts["confirm_prepared"] == 0
    assert session.response(identifier)["status"] == "paused"


def test_same_card_on_another_action_never_reuses_the_answers(monkeypatch):
    """E4: two action instances with the same card identity (and the same response key)
    keep separate gates; nothing the user answered on one applies to the other."""
    page, controller, session, identifier = start_chat(monkeypatch, NEW)
    draft_id = session.response(identifier)["payload"]["draft_id"]
    answer_gate(page, "domestic", identifier, answers=SAFE)
    assert _gate(controller, draft_id)[0] == "proceed"
    # A second action instance that shows the very same card (same payee, amount, tier).
    twin = "twin-" + draft_id
    controller._drafts[twin] = controller._drafts[draft_id]
    status = controller.scam_gate_establish(twin, text=None, origin={"response_id": identifier})
    assert status["card_identity"] == controller.scam_gate_status(draft_id)["card_identity"]
    assert status["decision"] == "incomplete" and status["answers"] == BLANK
    assert controller._scam_refusal(twin)["reason"] == "scam_incomplete"
    assert controller._scam_refusal(draft_id) is None
    # Answers for the first action, carrying its identity, are refused for the twin only
    # when they name the wrong action; each action's own answers stay its own.
    controller.scam_gate_answer(
        twin, card_identity=status["card_identity"], epoch=0, answers=GUIDED
    )
    assert _gate(controller, twin)[0] == "pause" and _gate(controller, draft_id)[0] == "proceed"
    assert controller._scam_refusal(twin)["reason"] == "scam_pause"
    assert controller._scam_refusal(draft_id) is None
    # Widget keys of the two actions under one response key never coincide.
    from corridor.ui.domestic_views import scam_gate_binding_key

    binding = page.session_state[scam_gate_binding_key(f"domestic_scam:{identifier}")]
    assert binding["identity"] == draft_id
    assert all(f":{draft_id}:" in key for key in binding["keys"].values())
    assert not any(f":{twin}:" in key for key in binding["keys"].values())
    # Chat actions never share an identity in the first place: a resend differs in its
    # per-action facts, so the new reply's questions start blank.
    send_message(page, NEW)
    second = page.session_state["domestic_response_id"]
    second_draft = session.response(second)["payload"]["draft_id"]
    assert second_draft != draft_id
    card_a, card_b = session.response(identifier)["card"], session.response(second)["card"]
    assert {"intent_id", "node_id", "action_hash"} <= {
        key for key in card_a if card_a.get(key) != card_b.get(key)
    }
    assert gate_values(page, "domestic", second) == BLANK
    assert controller.confirm_transfer(second_draft)["reason"] == "scam_incomplete"


def test_level_change_and_missing_context_invalidate_the_answers(monkeypatch):
    """E5: a context change (a lost utterance drops level 2 to 1) is an explicit re-grade
    that invalidates the answers; an ordinary redraw with the same context changes
    nothing and can never lower the level."""
    controller, session = _fresh()
    first = session.receive(FRAUD)
    draft_id = first["payload"]["draft_id"]
    words = controller.gateway.vault.tokenize(FRAUD)
    spy = Spy(controller, monkeypatch)
    status = controller.scam_gate_establish(draft_id, text=words, external_files=0)
    assert status["level"] == 2 and status["epoch"] == 0
    assert status["cues"] == ("authority", "urgency")
    identity = status["card_identity"]
    controller.scam_gate_answer(draft_id, card_identity=identity, epoch=0, answers=SAFE)
    assert _gate(controller, draft_id) == ("proceed", SAFE, 2, 0)
    # Ordinary redraws: idempotent.
    for _ in range(3):
        again = controller.scam_gate_establish(draft_id, text=words, external_files=0)
        assert (again["decision"], again["level"], again["epoch"]) == ("proceed", 2, 0)
    assert controller._scam_refusal(draft_id) is None
    # The utterance context goes missing: level 1 now, but only with new answers.
    regraded = controller.scam_gate_establish(draft_id, text=None, external_files=0)
    assert regraded["level"] == 1 and regraded["epoch"] == 1 and regraded["answers"] == BLANK
    assert regraded["decision"] == "incomplete" and regraded["cues"] == ()
    assert controller._scam_refusal(draft_id)["reason"] == "scam_incomplete"
    assert controller.confirm_transfer(draft_id)["reason"] == "scam_incomplete"
    # The old epoch's answers cannot be replayed into the new grade.
    controller.scam_gate_answer(draft_id, card_identity=identity, epoch=0, answers=SAFE)
    assert _gate(controller, draft_id) == ("incomplete", BLANK, 1, 1)
    # The context returns: another explicit re-grade back to level 2, still unanswered.
    back = controller.scam_gate_establish(draft_id, text=words, external_files=0)
    assert back["level"] == 2 and back["epoch"] == 2 and back["decision"] == "incomplete"
    # An attachment count change is a context change too.
    files = controller.scam_gate_establish(draft_id, text=words, external_files=1)
    assert files["epoch"] == 3 and files["context"]["external_files"] == 1
    assert spy.total() == 0 and not controller._pending


def test_regrade_to_level_zero_keeps_the_gate_and_asks_again(monkeypatch):
    """E5, level 1 -> 0 (third-round finding P1a): a known contact with an authority cue
    is level 1; once paused, a re-grade whose fresh grade is 0 (the words went missing)
    never becomes a blank "proceed": the gate stays at level 1 and incomplete, every
    entry point refuses with spies at zero, and only new answers open the path."""
    controller, session = _fresh()
    response = session.receive(KNOWN_AUTHORITY)
    identifier = response["response_id"]
    draft_id = response["payload"]["draft_id"]
    assert "new_recipient" not in response["card"]["tier_reasons"]
    words = controller.gateway.vault.tokenize(KNOWN_AUTHORITY)
    status = controller.scam_gate_establish(draft_id, text=words, external_files=0)
    assert status["level"] == 1 and status["cues"] == ("authority",)
    identity = status["card_identity"]
    controller.scam_gate_answer(draft_id, card_identity=identity, epoch=0, answers=GUIDED)
    assert _gate(controller, draft_id) == ("pause", GUIDED, 1, 0)
    spy = Spy(controller, monkeypatch)
    assert_all_refused(controller, session, identifier, draft_id, reason="scam_pause", spy=spy)
    # The same card without the words: the fresh grade is 0, the stored level stays 1.
    regraded = controller.scam_gate_establish(draft_id, text=None, external_files=0)
    assert (regraded["level"], regraded["epoch"], regraded["regrades"]) == (1, 1, 1)
    assert regraded["decision"] == "incomplete" and regraded["answers"] == BLANK
    assert regraded["cues"] == () and regraded["required"] and not regraded["stale"]
    assert controller._scam_refusal(draft_id)["reason"] == "scam_incomplete"
    assert_all_refused(controller, session, identifier, draft_id, reason="scam_incomplete", spy=spy)
    # The old epoch's answers cannot be replayed; an ordinary redraw changes nothing.
    controller.scam_gate_answer(draft_id, card_identity=identity, epoch=0, answers=SAFE)
    assert _gate(controller, draft_id) == ("incomplete", BLANK, 1, 1)
    for _ in range(2):
        again = controller.scam_gate_establish(draft_id, text=None, external_files=0)
        assert (again["level"], again["epoch"], again["decision"]) == (1, 1, "incomplete")
    assert controller.gateway.bank.execute_count == 0 and not controller._pending
    # New answers for the new epoch are judged by the level-1 rule: guiding pauses,
    # a retry opens the next epoch, safe answers proceed on the original path.
    controller.scam_gate_answer(draft_id, card_identity=identity, epoch=1, answers=GUIDED)
    assert _gate(controller, draft_id) == ("pause", GUIDED, 1, 1)
    assert controller.confirm_transfer(draft_id)["reason"] == "scam_pause"
    controller.scam_gate_retry(draft_id, card_identity=identity, epoch=1)
    controller.scam_gate_answer(draft_id, card_identity=identity, epoch=2, answers=SAFE)
    assert controller._scam_refusal(draft_id) is None
    assert spy.total() == 0
    result = controller.confirm_transfer(draft_id)
    assert result["status"] == "settled" and spy.counts["dispatch"] == 1
    assert controller.gateway.bank.execute_count == 1


def _state_with_a_new_lin():
    """The showcase state plus a third contact named Lin that is registered as new, so
    "给Lin转200元" clarifies among LIN_A, LIN_B (known) and LIN_C (new). The shipped
    fixture has no new payee sharing a known contact's name; it is not changed."""
    state = demo_state(showcase=True)
    base = state.contacts["CONTACT_LIN_B"]
    lin_c = Contact(
        id="CONTACT_LIN_C",
        owner=base.owner,
        name_token=base.name_token,
        phone_token="PHONE_LIN_C",
        aliases=["LIN_C"],
        destination_ref="DEST_CONTACT_LIN_C",
        destination_version=1,
        verified_at=base.verified_at,
        verification_ref="VERIFY_CONTACT_LIN_C",
        is_new=True,
        version=1,
    )
    return state.model_copy(update={"contacts": {**state.contacts, "CONTACT_LIN_C": lin_c}})


def test_payee_switch_from_a_new_to_a_known_contact_keeps_the_questions(monkeypatch):
    """E2, level 1 -> 0 on the real page (third-round finding P1b/P1c): the clarification
    card switches from a new contact (level 1, paused) to a known one with no cue in the
    words (fresh grade 0): the page keeps drawing the two questions, blank and enabled,
    shows no confirm button, and the controller refuses until they are answered again."""
    monkeypatch.setattr(
        "corridor.ui.controller.demo_state", lambda *args, **kwargs: _state_with_a_new_lin()
    )
    page, controller, session, identifier = start_chat(monkeypatch, CLARIFY_PLAIN)
    response = session.response(identifier)
    assert response["status"] == "clarify"
    assert set(response["payload"]["contacts"]) >= {"CONTACT_LIN_A", "CONTACT_LIN_C"}
    draft_id = response["payload"]["draft_id"]
    assert controller.state.contacts["CONTACT_LIN_C"].is_new
    assert not controller.state.contacts["CONTACT_LIN_A"].is_new
    _choose(page, "CONTACT_LIN_C")
    local = page.session_state[f"domestic_selected:{identifier}"]
    assert local["card"]["objects"]["contact_ref"] == "CONTACT_LIN_C"
    answer_gate(page, "domestic", identifier, answers=GUIDED)
    paused = controller.scam_gate_status(draft_id)
    assert (paused["decision"], paused["level"], paused["epoch"]) == ("pause", 1, 0)
    assert paused["cues"] == () and gate_disabled(page, "domestic", identifier) == {
        "verified": True,
        "guided": True,
    }
    assert "domestic_selected_confirm" not in button_keys(page)
    spy = Spy(controller, monkeypatch)
    _choose(page, "CONTACT_LIN_A")
    assert (
        page.session_state[f"domestic_selected:{identifier}"]["card"]["objects"]["contact_ref"]
        == "CONTACT_LIN_A"
    )
    rebound = controller.scam_gate_status(draft_id)
    assert rebound["card_identity"] != paused["card_identity"] and not rebound["stale"]
    assert (rebound["decision"], rebound["level"], rebound["epoch"]) == ("incomplete", 1, 1)
    assert rebound["answers"] == BLANK and rebound["regrades"] == 1 and rebound["required"]
    # The page: two blank, enabled questions; no confirm, no retry; Cancel stays.
    assert gate_values(page, "domestic", identifier) == BLANK
    assert gate_disabled(page, "domestic", identifier) == {"verified": False, "guided": False}
    assert "domestic_selected_confirm" not in button_keys(page)
    assert not any(key.endswith(":retry") for key in button_keys(page))
    assert "domestic_selected_cancel" in button_keys(page)
    text = shown_text(page)
    assert UI_MESSAGES["polish.scam.fact_known"]["zh-CN"] in text
    assert UI_MESSAGES["polish.scam.fact_new"]["zh-CN"] not in text
    assert UI_MESSAGES["polish.scam.incomplete"]["zh-CN"] in text
    assert UI_MESSAGES["polish.scam.pause_title"]["zh-CN"] not in text
    before = snapshot(controller, session)
    assert controller.confirm_transfer(draft_id)["reason"] == "scam_incomplete"
    assert spy.total() == 0 and controller.gateway.bank.execute_count == 0
    assert not controller._pending
    assert _without_calls(snapshot(controller, session)) == _without_calls(before)
    # A late callback for the new contact's card (old card, old epoch) changes nothing.
    controller.scam_gate_answer(
        draft_id, card_identity=paused["card_identity"], epoch=0, answers=SAFE
    )
    assert controller.scam_gate_rejections()[-1]["reason"] == "card_mismatch"
    assert _gate(controller, draft_id) == ("incomplete", BLANK, 1, 1)
    # Only new answers for the known contact's card open the path.
    answer_gate(page, "domestic", identifier, answers=SAFE)
    assert _gate(controller, draft_id) == ("proceed", SAFE, 1, 1)
    assert "domestic_selected_confirm" in button_keys(page)
    page.button(key="domestic_selected_confirm").click().run()
    assert not page.exception
    assert page.session_state[f"domestic_selected:{identifier}"]["status"] == "settled"
    assert spy.counts["confirm_transfer"] == 1 and spy.counts["dispatch"] == 1
    assert controller.gateway.bank.execute_count == 1


def test_pause_latches_until_the_retry_gesture(monkeypatch):
    """E6 (second-round finding R13): in the pause state the radios are disabled and an
    answer event for the paused epoch is refused; only the retry opens a new epoch."""
    page, controller, session, identifier = start_chat(monkeypatch, FRAUD)
    draft_id = session.response(identifier)["payload"]["draft_id"]
    answer_gate(page, "domestic", identifier, answers=GUIDED)
    assert _gate(controller, draft_id) == ("pause", GUIDED, 2, 0)
    assert gate_disabled(page, "domestic", identifier) == {"verified": True, "guided": True}
    spy = Spy(controller, monkeypatch)
    keys = gate_keys(page, "domestic", identifier)
    page.radio(key=keys["guided"]).set_value("no")
    page.run()
    assert not page.exception
    assert _gate(controller, draft_id) == ("pause", GUIDED, 2, 0)
    assert "domestic_confirm" not in button_keys(page)
    assert any(key.endswith(":retry") for key in button_keys(page))
    assert controller.confirm_transfer(draft_id)["reason"] == "scam_pause"
    assert "latched" in [item["reason"] for item in controller.scam_gate_rejections()]
    retry_gate(page)
    assert _gate(controller, draft_id) == ("incomplete", BLANK, 2, 1)
    assert gate_disabled(page, "domestic", identifier) == {"verified": False, "guided": False}
    answer_gate(page, "domestic", identifier, answers=SAFE)
    assert "domestic_confirm" in button_keys(page)
    assert spy.total() == 0


def test_pause_after_verified_mfa_and_the_challenge_window_boundary(monkeypatch):
    """E6: a pause after a completed verification refuses the final approval without a
    signature or dispatch; a pause keeps the challenge on its own 120 s window (119 s:
    still usable after the retry; 120 s: expired, no re-issue, cancel only)."""
    with monkeypatch.context() as patched:
        page, controller, session, identifier, _, (tier, card_hash) = _run_transfer(
            patched, gate="off"
        )
        off = outcome(page, controller, session, identifier, tier, card_hash)
    page, controller, session, identifier, pending_id = _pause_pending(
        monkeypatch, FRAUD, verified=True
    )
    challenge = controller._pending[pending_id]["challenge_id"]
    spy = Spy(controller, monkeypatch)
    assert_all_refused(controller, session, identifier, pending_id, reason="scam_pause", spy=spy)
    assert controller._pending[pending_id]["verified"]
    assert "domestic_confirm" not in button_keys(page)
    retry_gate(page)
    answer_gate(page, "domestic", identifier, answers=SAFE)
    assert "domestic_confirm" in button_keys(page)
    page.button(key="domestic_confirm").click().run()
    assert not page.exception
    # The same words give the same draft card, so the gate-off tuple is the reference.
    assert outcome(page, controller, session, identifier, off[1], off[2]) == off
    assert spy.counts["approve"] == 1 and spy.counts["dispatch"] == 1
    assert spy.counts["request_mfa"] == 0 and spy.counts["verify"] == 0
    assert challenge in controller.gateway.mfa._challenges
    # 119 s into the window: after the retry the same challenge still verifies.
    page, controller, session, identifier, pending_id = _pause_pending(
        monkeypatch, NEW, challenge=True
    )
    challenge = controller._pending[pending_id]["challenge_id"]
    code = controller.gateway.mfa.test_code(challenge)
    record = controller.gateway.mfa._challenges[challenge]
    spy = Spy(controller, monkeypatch)
    controller.advance_clock(119)
    assert_all_refused(
        controller, session, identifier, pending_id, reason="scam_pause", spy=spy, code=code
    )
    assert record.attempts == 0 and not record.closed
    retry_gate(page)
    answer_gate(page, "domestic", identifier, answers=SAFE)
    assert controller.request_mfa(pending_id)["challenge_id"] == challenge
    assert spy.counts["request_mfa"] == 0
    assert controller.complete_mfa(pending_id, code=code)["status"] == "mfa_verified"
    assert spy.counts["verify"] == 1 and spy.counts["submit_mfa"] == 1
    # 120 s: expired on its original window; the gate neither re-issues nor extends it.
    page, controller, session, identifier, pending_id = _pause_pending(
        monkeypatch, NEW, challenge=True
    )
    challenge = controller._pending[pending_id]["challenge_id"]
    code = controller.gateway.mfa.test_code(challenge)
    record = controller.gateway.mfa._challenges[challenge]
    spy = Spy(controller, monkeypatch)
    controller.advance_clock(120)
    page.run()
    assert not page.exception
    assert_all_refused(
        controller, session, identifier, pending_id, reason="scam_pause", spy=spy, code=code
    )
    assert record.attempts == 0
    retry_gate(page)
    answer_gate(page, "domestic", identifier, answers=SAFE)
    text = shown_text(page)
    assert "验证码已失效或已锁定" in text
    assert "domestic_verify_mfa:request" not in button_keys(page)
    assert not [item for item in page.text_input if item.key.endswith(":code")]
    assert controller.request_mfa(pending_id)["challenge_id"] == challenge
    assert spy.counts["request_mfa"] == 0
    assert controller.complete_mfa(pending_id, code=code)["status"] == "rejected"
    assert not controller._pending[pending_id]["verified"]
    assert spy.counts["dispatch"] == 0 and controller.gateway.bank.execute_count == 0
    assert "domestic_cancel" in button_keys(page)


def test_plan_paths_carry_no_payee_transfer_and_stay_checked(monkeypatch):
    """E7 (B): the only plan objects (``_plans``) are birthday plans: a budget lock and
    three scheduled transfers between the customer's own accounts or to catalogue
    merchants (no contact, never a new recipient), so no plan pays a payee and the chat
    never shows the questions for a plan. The entry points still check a gate written
    for a plan identity, and a plan the controller no longer holds refuses as stale."""
    controller = DemoController(initial_state=demo_state())
    controller.start_session()
    session = DomesticSession(controller)
    draft = controller.preview_birthday()
    plan_id = draft["plan_id"]
    node_ids = [node["id"] for node in draft["nodes"]]
    cards = list(controller._plans[plan_id]["display"].cards.values())
    assert len(cards) == 4
    assert [card["kind"] for card in cards] == ["lock_funds"] + ["schedule_transfer"] * 3
    for card in cards:
        assert card["objects"].get("contact_ref") is None
        assert "new_recipient" not in card["tier_reasons"]
        assert controller.scam_contact(card)[0] is None
        assert scam_friction.level(card, contact=None) == 0
    # The scheduled nodes pay the locked account back or a catalogue merchant, never a
    # contact: the registry has no entry for them and no plan node can be a new payee.
    assert {card["objects"].get("merchant_ref") for card in cards} == {None, "FLOWERS", "CAKE"}
    # Without words there is nothing to grade; the chat never renders the questions for a
    # plan reply (nodes), so no gate is established for a plan through the pages.
    assert controller.scam_gate_establish(plan_id) is None
    assert controller.scam_gate_status(plan_id) is None
    # A gate written for the plan identity is still honoured at confirm_plan and resume.
    spy = Spy(controller, monkeypatch)
    before = snapshot(controller, session)
    plan_identity = scam_friction.cards_identity(cards)
    controller._scam_gate.establish(plan_id, card_identity=plan_identity, level=1, context={})
    assert controller.confirm_plan(plan_id, node_ids)["reason"] == "scam_incomplete"
    assert controller.resume(plan_id)["reason"] == "scam_incomplete"
    controller.scam_gate_answer(plan_id, card_identity=plan_identity, epoch=0, answers=GUIDED)
    assert controller.confirm_plan(plan_id, node_ids)["reason"] == "scam_pause"
    assert controller.resume(plan_id)["reason"] == "scam_pause"
    assert spy.total() == 0 and _without_calls(snapshot(controller, session)) == _without_calls(
        before
    )
    controller.scam_gate_retry(plan_id, card_identity=plan_identity, epoch=0)
    controller.scam_gate_answer(plan_id, card_identity=plan_identity, epoch=1, answers=SAFE)
    assert controller.confirm_plan(plan_id, node_ids)["status"] == "settled"
    assert spy.counts["confirm_plan"] == 1
    # A plan identity the controller no longer holds refuses as stale, before the gateway.
    controller._scam_gate.establish("gone-plan", card_identity="x", level=1, context={})
    assert controller.resume("gone-plan")["reason"] == "scam_stale"
    assert spy.counts["resume"] == 0
    # Cancel never waits for the gate.
    controller, session = _fresh()
    response = session.receive(NEW)
    draft_id = response["payload"]["draft_id"]
    controller.scam_gate_establish(draft_id, text=None)
    assert session.receive("stop")["status"] in {"interrupted", "withdrawn", "no_active_plan"}
    assert session.response(response["response_id"])["status"] == "withdrawn"
    assert draft_id not in controller._drafts


def test_chat_plan_reply_renders_no_questions_and_establishes_no_gate(monkeypatch):
    """E7 (B): a plan reply (nodes) on the chat page never shows the questions, even when
    the words carry a cue; the plan is confirmed exactly as before S6."""
    page, controller, session, identifier = start_chat(
        monkeypatch, "反诈中心让我" + source_utterance("Plan a birthday")
    )
    response = session.response(identifier)
    payload = response["payload"].get("replacement", response["payload"])
    if not payload.get("nodes") and payload.get("candidates"):
        event_ref = payload["candidates"][0]["event_ref"]
        page.button(key=f"domestic_birthday:{identifier}:{event_ref}").click().run()
        assert not page.exception
        prune(page)
        identifier = page.session_state["domestic_response_id"]
        response = session.response(identifier)
        payload = response["payload"].get("replacement", response["payload"])
    assert payload.get("nodes"), response["status"]
    plan_id = payload["plan_id"]
    assert not gate_keys(page, "domestic", identifier)
    assert not [element for element in page.radio if element.key.startswith("domestic_scam:")]
    assert controller.scam_gate_status(plan_id) is None
    assert controller._scam_refusal(plan_id) is None
    assert all(card["objects"].get("contact_ref") is None for card in payload_cards(payload))
    spy = Spy(controller, monkeypatch)
    node_ids = [node["id"] for node in payload["nodes"]]
    result = session.confirm(identifier, selected_node_ids=node_ids)
    assert result["status"] != "paused"
    assert spy.counts["confirm_plan"] == 1


def payload_cards(payload):
    return [node["card"] for node in payload.get("nodes", [])]


def test_handoff_and_resume_of_a_paused_transfer_dispatch_nothing(monkeypatch):
    """E7 (B): a paused transfer handed off and resumed re-registers the plan only; the
    retired pending cannot be approved, and a new request gets fresh questions."""
    page, controller, session, identifier, pending_id = _pause_pending(monkeypatch, challenge=True)
    spy = Spy(controller, monkeypatch)
    plan_ids = controller._registered_plan_ids()
    assert plan_ids
    page.button(key="domestic_task_handoff").click().run()
    assert not page.exception
    prune(page)
    assert pending_id not in controller._pending
    resumed = controller.resume(plan_ids[-1])
    assert resumed["status"] == "resumed"
    assert spy.counts["resume"] == 1
    events = [row["event"] for row in controller.gateway.audit.records()]
    assert "plan_resumed" in events and "dispatch_intent" not in events
    assert controller.gateway.bank.execute_count == 0
    assert spy.counts["dispatch"] == 0 and spy.counts["approve"] == 0
    assert session.confirm(identifier)["status"] == "withdrawn"
    assert IMAdapter(session).click("verify_mfa", identifier)["status"] == "withdrawn"
    # The retired pending's gate stays (stale, harmless); the object itself is gone.
    assert controller.scam_gate_status(pending_id)["stale"]
    assert controller._scam_refusal(pending_id)["reason"] == "scam_stale"
    send_message(page, NEW)
    second = page.session_state["domestic_response_id"]
    assert gate_values(page, "domestic", second) == BLANK
    assert (
        controller.confirm_transfer(session.response(second)["payload"]["draft_id"])["reason"]
        == "scam_incomplete"
    )
    assert spy.counts["dispatch"] == 0


def test_redisplay_of_a_draft_pending_inherits_the_gate_before_any_gesture(monkeypatch):
    """E10 (third-round finding P4): a draft-mode pending (a scheduled transfer from
    chat) whose confirmation comes back as a redisplay gets a new pending id. The gate
    follows it at once (incomplete, the same level, cues, context and origin, its own
    card), so a direct gesture on the new id before any page redraw is refused with
    spies at zero; the page then draws blank questions for the new card and no confirm
    button, and only new answers let the confirmation reach the workflow again."""
    page, controller, session, identifier = start_chat(monkeypatch, SCHEDULED_NEW)
    response = session.response(identifier)
    old_id = response["payload"]["pending_id"]
    assert session._modes[identifier] == "pending"
    assert controller._pending[old_id]["mode"] == "draft" and old_id not in controller._drafts
    assert response["card"]["kind"] == "schedule_transfer" and response["card"]["tier"] == "red"
    answer_gate(page, "domestic", identifier, answers=SAFE)
    answered = controller.scam_gate_status(old_id)
    assert (answered["decision"], answered["level"], answered["epoch"]) == ("proceed", 1, 0)
    assert answered["origin"] == {
        "prefix": "domestic",
        "response_id": identifier,
        "route": "conversation",
    }
    assert "domestic_confirm" in button_keys(page)
    # The trusted renderer finds a changed fact at confirmation time (as the e2e drift
    # test does): the device workflow answers "redisplay".
    approve = controller.workflow._approve
    drift = {"left": 1}

    def changed(gateway, prepared, expected, display_id):
        if drift["left"]:
            drift["left"] -= 1
            prepared.proposal.card["amount"] = "301/1"
        return approve(gateway, prepared, expected, display_id)

    monkeypatch.setattr(controller.workflow, "_approve", changed)
    spy = Spy(controller, monkeypatch)
    first = session.confirm(identifier)
    new_id = first["payload"]["pending_id"]
    assert first["status"] == "redisplay" and new_id and new_id != old_id
    assert old_id not in controller._pending and controller._pending[new_id]["mode"] == "draft"
    assert spy.counts["confirm_plan"] == 1 and spy.total() == 1
    assert not controller.state.security.consumed_nonces
    inherited = controller.scam_gate_status(new_id)
    assert inherited["inherited_from"] == old_id and inherited["required"]
    assert (inherited["decision"], inherited["level"], inherited["epoch"]) == ("incomplete", 1, 0)
    assert inherited["answers"] == BLANK and not inherited["stale"]
    assert inherited["cues"] == answered["cues"] and inherited["context"] == answered["context"]
    assert inherited["origin"] == answered["origin"]
    assert inherited["card_identity"] != answered["card_identity"]
    assert controller.scam_gate_status(old_id)["stale"]
    assert controller._scam_refusal(old_id)["reason"] == "scam_stale"
    # The approval request the redisplayed confirmation registered (pre-S6 behaviour)
    # is the only one: the refused gestures below add none.
    budget = controller.gateway.approval_budget(controller.device.principal)
    # Before any redraw: every entry point for the migrated id is refused, nothing runs.
    assert_all_refused(controller, session, identifier, new_id, reason="scam_incomplete", spy=spy)
    assert controller._pending[new_id].get("challenge_id") is None
    assert controller.gateway.mfa._challenges == {}
    assert controller.gateway.approval_budget(controller.device.principal) == budget
    assert session.response(identifier)["payload"]["pending_id"] == new_id
    # The old epoch's answers (old id, old card) never apply to the new id.
    controller.scam_gate_answer(
        new_id, card_identity=answered["card_identity"], epoch=0, answers=SAFE
    )
    assert controller.scam_gate_rejections()[-1]["reason"] == "card_mismatch"
    assert _gate(controller, new_id) == ("incomplete", BLANK, 1, 0)
    # The page redraws: the same words re-establish idempotently (still epoch 0), the
    # questions are blank and enabled, there is no confirm button.
    page.run()
    assert not page.exception
    redrawn = controller.scam_gate_status(new_id)
    assert (redrawn["decision"], redrawn["epoch"], redrawn["regrades"]) == ("incomplete", 0, 0)
    assert gate_values(page, "domestic", identifier) == BLANK
    assert gate_disabled(page, "domestic", identifier) == {"verified": False, "guided": False}
    assert "domestic_confirm" not in button_keys(page)
    assert "domestic_cancel" in button_keys(page)
    assert spy.total() == 0
    # New answers for the new card open the path: the confirmation reaches the workflow
    # for the first time since the redisplay and the pending moves on to MFA.
    answer_gate(page, "domestic", identifier, answers=SAFE)
    assert _gate(controller, new_id) == ("proceed", SAFE, 1, 0)
    assert "domestic_confirm" in button_keys(page)
    page.button(key="domestic_confirm").click().run()
    assert not page.exception
    assert session.response(identifier)["status"] == "waiting_mfa"
    assert controller._pending[new_id]["mode"] == "prepared"
    assert spy.counts["confirm_plan"] == 1 and spy.counts["request_mfa"] == 0
    assert spy.counts["dispatch"] == 0 and controller.gateway.bank.execute_count == 0
    # The prepared card of the same intent differs only in its draft digest: the
    # answers given for the new card still hold, so the code button is offered.
    assert controller._scam_refusal(new_id) is None
    assert "domestic_verify_mfa:request" in button_keys(page)


def test_page_language_and_view_changes_keep_the_projection(monkeypatch):
    """E8: a page change, a language switch (second-round finding R9) and a view toggle
    keep the gate and re-seed the widgets from the record, in the pause and in the
    proceed state."""
    page, controller, session, identifier, pending_id = _pause_pending(monkeypatch, FRAUD)
    spy = Spy(controller, monkeypatch)
    before = snapshot(controller, session)
    for route in ("device", "conversation", "billing", "conversation", "im", "conversation"):
        navigate(page, route)
        assert not page.exception, route
        assert _gate(controller, pending_id) == ("pause", GUIDED, 2, 0), route
        assert "domestic_verify_mfa:request" not in button_keys(page), route
        assert f"device_mfa:{pending_id}:request" not in button_keys(page), route
        assert snapshot(controller, session) == before, route
    assert gate_values(page, "domestic", identifier) == GUIDED
    # Language switch while paused: the widgets show the recorded answers, disabled.
    page.selectbox(key="locale").select("en").run()
    assert not page.exception
    assert gate_values(page, "domestic", identifier) == GUIDED
    assert gate_disabled(page, "domestic", identifier) == {"verified": True, "guided": True}
    assert "domestic_verify_mfa:request" not in button_keys(page)
    assert UI_MESSAGES["polish.scam.trigger.guided_yes"]["en"] in shown_text(page)
    assert _gate(controller, pending_id) == ("pause", GUIDED, 2, 0)
    page.selectbox(key="locale").select("zh-CN").run()
    assert gate_values(page, "domestic", identifier) == GUIDED
    assert UI_MESSAGES["polish.scam.trigger.guided_yes"]["zh-CN"] in shown_text(page)
    # Proceed state across a switch: the next-step button and the answers agree.
    retry_gate(page)
    answer_gate(page, "domestic", identifier, answers=SAFE)
    assert "domestic_verify_mfa:request" in button_keys(page)
    page.selectbox(key="locale").select("en").run()
    assert gate_values(page, "domestic", identifier) == SAFE
    assert "domestic_verify_mfa:request" in button_keys(page)
    assert "gate state = may continue" in shown_text(page)
    page.selectbox(key="locale").select("zh-CN").run()
    assert gate_values(page, "domestic", identifier) == SAFE
    # View toggles keep the pause; the record line is judge-only.
    answer_gate(page, "domestic", identifier, answers={"guided": "yes"})
    assert _gate(controller, pending_id)[0] == "pause"
    for _ in range(2):
        page.button(key="view_toggle").click().run()
        assert not page.exception
        view = page.session_state["polish_view"]
        text = shown_text(page)
        assert ("交互记录" in text) == (view == "judge")
        assert "domestic_verify_mfa:request" not in button_keys(page)
        assert gate_values(page, "domestic", identifier) == GUIDED
        assert _gate(controller, pending_id)[0] == "pause"
    assert spy.total() == 0
    assert controller.request_mfa(pending_id)["status"] == "paused"


@pytest.mark.parametrize("reason", ["scam_incomplete", "scam_pause", "scam_stale"])
def test_every_refusal_branch_leaves_bank_state_hashes_chain_and_counts_unchanged(
    monkeypatch, reason
):
    """E9: for each refusal reason, every entry point leaves the bank state, the
    protected hashes, the audit head, the challenge records and every spied call
    count unchanged; the reply envelope carries the reason and nothing else moves."""
    controller, session = _fresh()
    if reason == "scam_stale":
        response = session.receive(CLARIFY)
        draft_id = response["payload"]["draft_id"]
        controller.select_contact(draft_id, "CONTACT_LIN_B")
        controller.scam_gate_establish(draft_id, text=controller.gateway.vault.tokenize(CLARIFY))
        controller.select_contact(draft_id, "CONTACT_LIN_A")
        assert controller.scam_gate_status(draft_id)["stale"]
    else:
        response = session.receive(FRAUD)
        draft_id = response["payload"]["draft_id"]
        status = controller.scam_gate_establish(
            draft_id, text=controller.gateway.vault.tokenize(FRAUD)
        )
        if reason == "scam_pause":
            controller.scam_gate_answer(
                draft_id, card_identity=status["card_identity"], epoch=0, answers=UNSURE
            )
    identifier = response["response_id"]
    spy = Spy(controller, monkeypatch)
    assert_all_refused(controller, session, identifier, draft_id, reason=reason, spy=spy)
    assert not controller._pending and draft_id in controller._drafts
    assert controller.gateway.approval_budget(controller.device.principal)["used"] == 0
    assert controller.gateway.bank.execute_count == 0
    assert controller.gateway.mfa._challenges == {}
    envelope = session.response(identifier)
    if session._modes.get(identifier) in {"transfer", "pending"}:
        assert envelope["status"] == "paused" and envelope["payload"]["reason"] == reason
    else:
        # The clarification reply is confirmed through the controller directly; the
        # session envelope stays as it was.
        assert envelope["status"] == "clarify"
    assert envelope["buttons"] == [] or all(
        button["id"] in {"confirm", "verify_mfa"} for button in envelope["buttons"]
    )
    # The same for a pending with a challenge and a verified MFA (pause reason).
    if reason == "scam_pause":
        page, controller, session, identifier, pending_id = _pause_pending(
            monkeypatch, NEW, verified=True
        )
        spy = Spy(controller, monkeypatch)
        assert_all_refused(controller, session, identifier, pending_id, reason=reason, spy=spy)
        assert controller._pending[pending_id]["verified"]
        assert controller.gateway.bank.execute_count == 0


# --- review fixes kept from round one ----------------------------------------------------


def test_pause_on_a_draft_survives_page_changes_and_replayed_gestures(monkeypatch):
    page, controller, session, identifier = start_chat(monkeypatch)
    draft_id = session.response(identifier)["payload"]["draft_id"]
    answer_gate(page, "domestic", identifier, answers=GUIDED)
    assert _gate(controller, draft_id)[0] == "pause"
    spy = Spy(controller, monkeypatch)
    for route in ("billing", "conversation", "transfers", "conversation"):
        navigate(page, route)
        assert not page.exception, route
        assert _gate(controller, draft_id) == ("pause", GUIDED, 2, 0), route
    assert "domestic_confirm" not in button_keys(page)
    assert gate_values(page, "domestic", identifier) == GUIDED
    assert_all_refused(controller, session, identifier, draft_id, reason="scam_pause", spy=spy)
    assert controller.gateway.approval_budget(controller.device.principal)["used"] == 0
    assert not controller._pending and draft_id in controller._drafts
    # A draft-state pause: the chat page's hint names the reply's own Cancel, not the
    # device page, which has no row for an unconfirmed draft.
    send_message(page, "查询余额")
    assert UI_MESSAGES["polish.scam.other_gated"]["zh-CN"] in shown_text(page)
    navigate(page, "device")
    assert not any(draft_id in key for key in button_keys(page))


def test_unanswered_questions_are_refused_at_the_controller(monkeypatch):
    page, controller, session, identifier = start_chat(monkeypatch, NEW)
    draft_id = session.response(identifier)["payload"]["draft_id"]
    assert set(gate_keys(page, "domestic", identifier)) == {"verified", "guided"}
    assert "domestic_confirm" not in button_keys(page)
    assert _gate(controller, draft_id) == ("incomplete", BLANK, 1, 0)
    spy = Spy(controller, monkeypatch)
    assert_all_refused(controller, session, identifier, draft_id, reason="scam_incomplete", spy=spy)
    assert session.response(identifier)["status"] == "paused"
    assert not controller._pending
    page.run()
    assert UI_MESSAGES["polish.scam.incomplete_copy"]["zh-CN"] in shown_text(page)
    # One answer is still incomplete; both safe answers let the same draft continue.
    answer_gate(page, "domestic", identifier, answers={"verified": "yes"})
    assert controller.confirm_transfer(draft_id)["reason"] == "scam_incomplete"
    answer_gate(page, "domestic", identifier, answers=SAFE)
    assert "domestic_confirm" in button_keys(page)
    assert session.confirm(identifier)["status"] == "waiting_mfa"
    assert session.response(identifier)["payload"]["pending_id"] == draft_id
    assert spy.counts["confirm_transfer"] == 1


def test_paused_copy_never_claims_that_no_challenge_was_issued(monkeypatch):
    """A pause can follow an issued challenge: the bubble speaks only of new requests."""
    page, controller, session, identifier, pending_id = _pause_pending(
        monkeypatch, NEW, challenge=True
    )
    challenge = controller._pending[pending_id]["challenge_id"]
    # An old tab replays the confirmation: refused, and the bubble shows the paused copy.
    assert session.confirm(identifier)["status"] == "paused"
    page.run()
    assert not page.exception
    text = shown_text(page)
    copy = UI_MESSAGES["polish.scam.paused_copy"]
    assert copy["zh-CN"] in text
    assert UI_MESSAGES["polish.scam.pause_challenge"]["zh-CN"] in text
    assert "没有发起" not in copy["zh-CN"] and "不会再发起新的短信验证" in copy["zh-CN"]
    assert "new sms verification" in copy["en"].lower()
    assert "has been requested" not in copy["en"] and "nothing has been submitted" not in copy["en"]
    # The challenge issued before the pause is kept, neither voided nor renewed.
    assert controller._pending[pending_id]["challenge_id"] == challenge
    assert challenge in controller.gateway.mfa._challenges
    for key in (
        "polish.scam.pause",
        "polish.scam.pause_authority",
        "polish.scam.device_paused",
        "polish.scam.device_incomplete",
        "polish.scam.device_stale",
        "polish.scam.incomplete_copy",
        "polish.scam.stale_copy",
        "polish.scam.incomplete",
        "polish.scam.stale",
    ):
        assert "没有发起" not in UI_MESSAGES[key]["zh-CN"], key
        assert "不会" in UI_MESSAGES[key]["zh-CN"], key


def test_level_rules_for_the_redesigned_first_question(monkeypatch):
    """Level 1 (no cue): not verified / unsure still proceed, someone guiding pauses with
    the plain copy. Level 2 (a cue in the words): not verified and unsure pause too."""
    page, controller, session, identifier = start_chat(monkeypatch, NEW)
    draft_id = session.response(identifier)["payload"]["draft_id"]
    answer_gate(page, "domestic", identifier, answers=UNVERIFIED)
    assert _gate(controller, draft_id) == ("proceed", UNVERIFIED, 1, 0)
    assert "domestic_confirm" in button_keys(page)
    assert UI_MESSAGES["polish.scam.pause_title"]["zh-CN"] not in shown_text(page)
    answer_gate(page, "domestic", identifier, answers=UNSURE)
    assert _gate(controller, draft_id)[0] == "proceed"
    answer_gate(page, "domestic", identifier, answers={"guided": "yes"})
    assert _gate(controller, draft_id)[0] == "pause"
    text = shown_text(page)
    assert "domestic_confirm" not in button_keys(page)
    assert UI_MESSAGES["polish.scam.trigger.guided_yes"]["zh-CN"] in text
    assert UI_MESSAGES["polish.scam.trigger.verified_unsure"]["zh-CN"] not in text
    assert "挂断电话" not in text and "银行、公安" not in text
    assert UI_MESSAGES["polish.scam.cues"]["zh-CN"].split("{")[0] not in text
    # R5: guided alone -> end the guidance and decide alone; no "official channel" title
    # or button, since there is nothing for this user to verify.
    assert UI_MESSAGES["polish.scam.pause_title_guided"]["zh-CN"] in text
    assert UI_MESSAGES["polish.scam.pause_title"]["zh-CN"] not in text
    assert "官方渠道" not in text
    labels = {button.label for button in page.button}
    assert UI_MESSAGES["polish.scam.pause_retry_guided"]["zh-CN"] in labels
    assert UI_MESSAGES["polish.scam.pause_retry"]["zh-CN"] not in labels
    # Level 2 (a cue in the user's words): not verified pauses with the hang-up line.
    page, controller, session, identifier = start_chat(monkeypatch)
    draft_id = session.response(identifier)["payload"]["draft_id"]
    answer_gate(page, "domestic", identifier, answers=UNVERIFIED)
    assert _gate(controller, draft_id) == ("pause", UNVERIFIED, 2, 0)
    assert "domestic_confirm" not in button_keys(page)
    text = shown_text(page)
    assert UI_MESSAGES["polish.scam.trigger.verified_no"]["zh-CN"] in text
    assert "挂断电话" in text
    assert UI_MESSAGES["polish.fraud.authority"]["zh-CN"] in text
    # A verification trigger keeps the verification title and button.
    assert UI_MESSAGES["polish.scam.pause_title"]["zh-CN"] in text
    assert UI_MESSAGES["polish.scam.pause_title_guided"]["zh-CN"] not in text
    assert UI_MESSAGES["polish.scam.pause_retry"]["zh-CN"] in {b.label for b in page.button}
    retry_gate(page)
    answer_gate(page, "domestic", identifier, answers=UNSURE)
    assert _gate(controller, draft_id)[0] == "pause"
    assert UI_MESSAGES["polish.scam.trigger.verified_unsure"]["zh-CN"] in shown_text(page)
    # The same words with an external cue only (no authority) pause with the plain copy.
    page, controller, session, identifier = start_chat(
        monkeypatch, "给新联系人转480元，账号是对方发来的"
    )
    draft_id = session.response(identifier)["payload"]["draft_id"]
    answer_gate(page, "domestic", identifier, answers=UNVERIFIED)
    assert _gate(controller, draft_id)[0] == "pause"
    text = shown_text(page)
    assert "挂断电话" not in text
    assert UI_MESSAGES["polish.scam.trigger.verified_no"]["zh-CN"] in text


def test_a_gated_reply_stays_reachable_after_another_message(monkeypatch):
    """P3: after another request the paused reply is no longer current; both the chat
    page and the device page lead back to it (the device page by the gate's own
    record of where the questions were shown)."""
    page, controller, session, identifier, pending_id = _pause_pending(monkeypatch, NEW)
    send_message(page, "查询余额")
    assert page.session_state["domestic_response_id"] != identifier
    assert not gate_keys(page, "domestic", identifier)
    assert UI_MESSAGES["polish.scam.other_gated"]["zh-CN"] in shown_text(page)
    assert "domestic_scam_return" in button_keys(page)
    origin = controller.scam_gate_status(pending_id)["origin"]
    assert origin == {"prefix": "domestic", "response_id": identifier, "route": "conversation"}
    navigate(page, "device")
    assert not page.exception
    keys = button_keys(page)
    assert f"scam_back:{pending_id}" in keys and f"cancel:{pending_id}" in keys
    assert f"device_mfa:{pending_id}:request" not in keys and f"approve:{pending_id}" not in keys
    page.button(key=f"scam_back:{pending_id}").click().run()
    assert not page.exception
    assert page.session_state["active_page"] == "conversation"
    assert page.session_state["domestic_response_id"] == identifier
    assert gate_values(page, "domestic", identifier) == GUIDED
    assert "domestic_scam_return" not in button_keys(page)
    assert _gate(controller, pending_id)[0] == "pause"
    # The chat page's own way back works the same when the reply is not current.
    send_message(page, "查询余额")
    assert page.session_state["domestic_response_id"] != identifier
    page.button(key="domestic_scam_return").click().run()
    assert not page.exception
    assert page.session_state["domestic_response_id"] == identifier
    retry_gate(page)
    answer_gate(page, "domestic", identifier, answers=SAFE)
    assert "domestic_verify_mfa:request" in button_keys(page)
    assert _gate(controller, pending_id)[0] == "proceed"
    # The transfer itself never moved: no challenge, no verification, no dispatch.
    assert session.response(identifier)["status"] == "waiting_mfa"
    assert controller._pending[pending_id]["challenge_id"] is None
    assert not controller._pending[pending_id]["verified"]
    assert "approval_consumed" not in [row["event"] for row in controller.gateway.audit.records()]


def test_device_page_returns_to_the_im_page_when_the_questions_were_shown_there(monkeypatch):
    """The way back is not hard-wired to the chat page: a check shown on the simulated IM
    page sends the device page back to that page and that reply."""
    monkeypatch.setenv("CORRIDOR_LOCALE", "zh-CN")
    page = app()
    page.button(key="enter").click().run()
    navigate(page, "im")
    page.text_input(key="im_text").set_value(NEW)
    page.button(key="im_send").click().run()
    assert not page.exception
    controller = page.session_state["controller"]
    session = page.session_state["domestic_session"]
    identifier = page.session_state["im_response_id"]
    assert set(gate_keys(page, "im", identifier)) == {"verified", "guided"}
    answer_gate(page, "im", identifier, answers=SAFE)
    page.button(key="im_confirm").click().run()
    assert not page.exception
    pending_id = session.response(identifier)["payload"]["pending_id"]
    answer_gate(page, "im", identifier, answers=GUIDED)
    assert controller.scam_gate_status(pending_id)["origin"] == {
        "prefix": "im",
        "response_id": identifier,
        "route": "im",
    }
    navigate(page, "device")
    assert f"scam_back:{pending_id}" in button_keys(page)
    assert f"approve:{pending_id}" not in button_keys(page)
    page.button(key=f"scam_back:{pending_id}").click().run()
    assert not page.exception
    assert page.session_state["active_page"] == "im"
    assert page.session_state["im_response_id"] == identifier
    assert gate_values(page, "im", identifier) == GUIDED
    assert controller.request_mfa(pending_id)["status"] == "paused"


def test_known_contact_with_an_authority_cue_shows_facts_and_the_cue(monkeypatch):
    page, controller, session, identifier = start_chat(monkeypatch, KNOWN_AUTHORITY)
    response = session.response(identifier)
    assert response["card"]["kind"] == "transfer"
    assert "new_recipient" not in response["card"]["tier_reasons"]
    assert set(gate_keys(page, "domestic", identifier)) == {"verified", "guided"}
    text = shown_text(page)
    assert UI_MESSAGES["polish.scam.facts_title"]["zh-CN"] in text
    assert UI_MESSAGES["polish.scam.fact_known"]["zh-CN"] in text
    assert UI_MESSAGES["polish.scam.fact_new"]["zh-CN"] not in text
    assert UI_MESSAGES["polish.scam.fact_source"]["zh-CN"] in text
    assert UI_MESSAGES["polish.scam.cue.authority"]["zh-CN"] in text
    draft_id = response["payload"]["draft_id"]
    assert _gate(controller, draft_id)[2] == 1
    # The level-1 rule: not verified proceeds, someone guiding pauses (hang-up line).
    answer_gate(page, "domestic", identifier, answers=UNVERIFIED)
    assert "domestic_confirm" in button_keys(page)
    answer_gate(page, "domestic", identifier, answers=GUIDED)
    assert "domestic_confirm" not in button_keys(page)
    text = shown_text(page)
    # R5: guided alone with an authority cue: end the guidance, decide alone, and the
    # official-number line stays. R6: the button then covers both statements the action
    # sentence asks for ("confirmed on my own and verified by the official number").
    assert UI_MESSAGES["polish.scam.pause_title_guided"]["zh-CN"] in text
    assert UI_MESSAGES["polish.scam.pause_title"]["zh-CN"] not in text
    assert "挂断电话" in text and "银行卡背面的官方客服电话" in text
    assert "挂断或结束对方的指导" in text and "是您本人的意愿" in text
    labels = {button.label for button in page.button}
    assert UI_MESSAGES["polish.scam.pause_retry_guided_authority"]["zh-CN"] in labels
    assert UI_MESSAGES["polish.scam.pause_retry_guided"]["zh-CN"] not in labels
    assert UI_MESSAGES["polish.scam.pause_retry"]["zh-CN"] not in labels


def test_an_everyday_word_towards_a_new_payee_stays_at_level_one(monkeypatch):
    """R5 (third honesty review, P7): 马上 or 分两笔 in the user's words no longer raise a
    payment to a new payee to level 2. The cue sentence still names the matched rule, an
    honest "not verified" may continue, and a pause by "someone guiding" alone uses the
    guided wording; the drill utterance (authority and external cues) stays level 2."""
    page, controller, session, identifier = start_chat(monkeypatch, "马上给新联系人转480元")
    response = session.response(identifier)
    draft_id = response["payload"]["draft_id"]
    assert response["tier"] == "red" and response["card"]["tier_reasons"] == ["new_recipient"]
    status = controller.scam_gate_status(draft_id)
    assert status["level"] == 1 and tuple(status["cues"]) == ("urgency",)
    text = shown_text(page)
    assert UI_MESSAGES["polish.scam.cue.urgency"]["zh-CN"] in text
    assert UI_MESSAGES["polish.scam.fact_new"]["zh-CN"] in text
    assert "domestic_confirm" not in button_keys(page)
    answer_gate(page, "domestic", identifier, answers=UNVERIFIED)
    assert _gate(controller, draft_id) == ("proceed", UNVERIFIED, 1, 0)
    assert "domestic_confirm" in button_keys(page)
    assert UI_MESSAGES["polish.scam.pause_title"]["zh-CN"] not in shown_text(page)
    answer_gate(page, "domestic", identifier, answers={"guided": "yes"})
    assert _gate(controller, draft_id) == ("pause", {"verified": "no", "guided": "yes"}, 1, 0)
    text = shown_text(page)
    assert "domestic_confirm" not in button_keys(page)
    assert UI_MESSAGES["polish.scam.pause_title_guided"]["zh-CN"] in text
    assert UI_MESSAGES["polish.scam.trigger.guided_yes"]["zh-CN"] in text
    assert UI_MESSAGES["polish.scam.trigger.verified_no"]["zh-CN"] not in text
    assert "官方渠道" not in text and "挂断电话" not in text
    labels = {button.label for button in page.button}
    assert UI_MESSAGES["polish.scam.pause_retry_guided"]["zh-CN"] in labels
    assert UI_MESSAGES["polish.scam.pause_retry"]["zh-CN"] not in labels
    assert gate_disabled(page, "domestic", identifier) == {"verified": True, "guided": True}
    assert controller.gateway.bank.execute_count == 0
    drill = controller.gateway.vault.tokenize(source_utterance("polish.scam.drill_utterance"))
    contact = controller.state.contacts["CONTACT_NEW"]
    assert scam_friction.level(response["card"], contact=contact, text=drill) == 2


def test_drill_bubble_uses_the_display_text_and_hides_the_case_number(monkeypatch):
    monkeypatch.setenv("CORRIDOR_DEFAULT_VIEW", "customer")
    monkeypatch.setenv("CORRIDOR_LOCALE", "zh-CN")
    page = app()
    page.button(key="enter").click().run()
    captions = "\n".join(str(item.value) for item in page.caption)
    assert UI_MESSAGES["polish.scam.drill_caption"]["zh-CN"] in captions
    assert "zh-q-01" not in captions
    page.button(key="domestic_example_scam_drill").click().run()
    assert not page.exception
    transcript = page.session_state["domestic_transcript"]
    assert transcript[-1]["text"] == UI_MESSAGES["polish.scam.drill_utterance"]["zh-CN"]
    identifier = page.session_state["domestic_response_id"]
    # R7: the drill is held by the phrase gate (no card, no questions).
    assert not gate_keys(page, "domestic", identifier)
    held = page.session_state["domestic_session"].response(identifier)
    assert held["payload"]["scam_phrase"]
    monkeypatch.setenv("CORRIDOR_DEFAULT_VIEW", "judge")
    page = app()
    page.button(key="enter").click().run()
    captions = "\n".join(str(item.value) for item in page.caption)
    assert "zh-q-01" in captions
    monkeypatch.setenv("CORRIDOR_LOCALE", "en")
    page = app()
    page.button(key="enter").click().run()
    page.button(key="domestic_example_scam_drill").click().run()
    assert not page.exception
    transcript = page.session_state["domestic_transcript"]
    assert transcript[-1]["text"] == UI_MESSAGES["polish.scam.drill_utterance"]["en"]
    captions = "\n".join(str(item.value) for item in page.caption)
    assert "no payment happens unless you act" in captions


def test_paused_task_node_waits_for_answers_not_for_a_device():
    from corridor.ui.dag import NODE_STYLES, dag_svg
    from corridor.ui.domestic_views import task_node_status

    assert task_node_status({"status": "paused", "buttons": [{"id": "confirm"}]}) == "paused"
    assert task_node_status({"status": "paused", "buttons": []}) == "paused"
    assert NODE_STYLES["paused"] == ("#fff1f1", "Awaiting the recipient check answers")
    assert (
        UI_MESSAGES["Awaiting the recipient check answers"]["zh-CN"] == "等待您作答（核实收款人）"
    )
    snapshot = {
        "plan": {"nodes": [{"node_id": "current", "tool": "transfer", "depends_on": []}]},
        "nodes": {"current": {"status": "paused"}},
    }
    svg = dag_svg(snapshot)
    assert 'data-status="paused"' in svg and "#fff1f1" in svg
    assert 'data-status="waiting_approval"' not in svg
