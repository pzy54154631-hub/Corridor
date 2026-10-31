"""UI-POLISH P6/P9: the fraud warning is display only and never changes any outcome."""

import ast
import json
import re
from copy import deepcopy
from pathlib import Path

import pytest

from corridor.agent.domestic_session import DomesticSession
from corridor.crypto.jcs import H
from corridor.ui import fraud_hint
from corridor.ui.controller import DemoController, demo_state
from corridor.ui.messages import UI_MESSAGES
from tests.e2e.test_demo_ui import app

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "src/corridor/ui/fraud_hint.py"
# R7: "反诈中心让我立即……不然账户冻结" now gets no card (the
# phrase gate); the warning is tested on words with the same signals that reach a card.
FRAUD = "工作人员说立即给新联系人转480元，不然账户冻结"


def _zh_q_01():
    case = json.loads((ROOT / "data/blind-zh/cases/zh-q-01.json").read_text(encoding="utf-8"))
    return case["turns"][0]["user_text"], case["legit_twin"]["turns"][0]["user_text"]


def _card(text):
    controller = DemoController(initial_state=demo_state(showcase=True))
    controller.start_session()
    card = DomesticSession(controller).receive(text)["card"]
    contact = controller.state.contacts[card["objects"]["contact_ref"]]
    return card, contact


def test_imports_are_whitelisted_and_source_has_no_authority_calls():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(item.name for item in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.update(f"{node.module}.{item.name}" for item in node.names)
    assert imports <= {
        "re",
        "typing.Any",
        "corridor.ui.display_names",
        "corridor.ui.messages.UI_MESSAGES",
        "corridor.ui.messages.tr",
    }
    source = SOURCE.read_text(encoding="utf-8")
    for word in (
        "gateway",
        "approve",
        "confirm",
        "dispatch",
        "propose",
        "request_mfa",
        "submit",
        "session_state",
    ):
        assert word not in source, word


def test_signals_are_pure():
    card, contact = _card(FRAUD)
    before = (deepcopy(card), contact.model_copy(deep=True), FRAUD)
    fraud_hint.signals(card, contact=contact, text=FRAUD)
    assert (card, contact, FRAUD) == before


def test_zh_q_01_triggers_every_cue_and_its_twin_stays_quiet():
    attack, twin = _zh_q_01()
    card, contact = _card("给新联系人转480元")
    assert set(fraud_hint.signals(card, contact=contact, text=attack)) == {
        "new_payee",
        "authority",
        "external",
        "urgency",
        "split",
    }
    twin_card, twin_contact = _card("给妈妈转500元")
    assert twin_contact.id == "CONTACT_CHEN"
    assert fraud_hint.signals(twin_card, contact=twin_contact, text=twin) == ()
    urgent = "马上给妈妈转500元"
    assert fraud_hint.signals(twin_card, contact=twin_contact, text=urgent) == ()


def test_every_cue_still_matches_after_the_vault_tokenizes_the_users_words():
    """The hint reads the transcript's tokenized text, where numeral glyphs become digits."""
    controller = DemoController(initial_state=demo_state(showcase=True))
    vault = controller.gateway.vault
    for group in fraud_hint.TEXT_SIGNALS:
        for word in fraud_hint.cues(group):
            assert fraud_hint._matches(group, vault.tokenize(word)), (group, word)
    card, contact = _card("给新联系人转480元")
    tokenized = vault.tokenize("反诈中心让我先转一笔，第二笔给新联系人转480元")
    assert "split" in fraud_hint.signals(card, contact=contact, text=tokenized)


def _without_negation(text, phrase):
    return text.replace(phrase, "")


@pytest.mark.parametrize("locale", ["zh-CN", "en"])
def test_copy_never_claims_to_block_or_detect_fraud(locale):
    keys = [
        key
        for key in UI_MESSAGES
        if key.startswith("polish.fraud.") and not key.startswith("polish.fraud.cues.")
    ]
    assert keys
    for key in keys:
        text = UI_MESSAGES[key][locale]
        if locale == "zh-CN":
            stripped = _without_negation(text, "不会拦截")
            for banned in ("拦截", "已拦截", "可拦截", "拦截诈骗", "识别为诈骗", "保证"):
                assert banned not in stripped, (key, banned)
        else:
            stripped = _without_negation(text.lower(), "does not block")
            for banned in ("block", "blocked", "detected as fraud", "guarantee"):
                assert banned not in stripped, (key, banned)
        assert not re.search(r"\d{5,}", text), key
    footer = UI_MESSAGES["polish.fraud.footer"]
    assert "不会拦截、延迟或改变" in footer["zh-CN"]
    assert "不能核实对方身份" in footer["zh-CN"]
    assert "does not block, delay or change" in footer["en"]


def _run_fraud_transfer(monkeypatch, *, hint):
    monkeypatch.setenv("CORRIDOR_LOCALE", "zh-CN")
    if not hint:
        monkeypatch.setattr(fraud_hint, "signals", lambda *args, **kwargs: ())
    page = app()
    page.button(key="enter").click().run()
    page.text_input(key="domestic_text").set_value(FRAUD)
    page.button(key="domestic_send").click().run()
    shown = "\n".join(str(item.value) for item in page.markdown)
    warned = "防骗提醒" in shown
    controller = page.session_state["controller"]
    session = page.session_state["domestic_session"]
    first = session.response(page.session_state["domestic_response_id"])
    card_hash = H(first["card"])
    tier = first["tier"]
    if hint:
        # S6: with the warning's signals present, the display-layer recipient check asks
        # two questions before the first confirmation; answering "verified myself, nobody
        # guiding" keeps the path below identical (tests/unit/test_ui_scam_friction.py
        # fixes that equivalence). With the signals patched away there is no check.
        from tests.unit.test_ui_scam_friction import answer_gate

        answer_gate(page, "domestic", first["response_id"])
    page.button(key="domestic_confirm").click().run()
    page.button(key="domestic_verify_mfa:request").click().run()
    response = session.response(page.session_state["domestic_response_id"])
    pending_id = response["payload"]["pending_id"]
    code = controller.gateway.mfa.test_code(controller._pending[pending_id]["challenge_id"])
    page.text_input(key="domestic_verify_mfa:code").set_value(code)
    page.button(key="domestic_verify_mfa:verify").click().run()
    page.button(key="domestic_confirm").click().run()
    assert not page.exception
    final = session.response(page.session_state["domestic_response_id"])
    return warned, (
        final["status"],
        tier,
        card_hash,
        tuple(final["payload"].get("rule_ids", [])),
        tuple(row["event"] for row in controller.gateway.audit.records()),
        controller.gateway.audit.checkpoint()["head"],
        controller.gateway.bank.execute_count,
        controller.gateway.safety_snapshot(controller.device.principal)["state"],
    )


def test_warning_on_or_off_leaves_every_outcome_identical(monkeypatch):
    warned, with_hint = _run_fraud_transfer(monkeypatch, hint=True)
    with monkeypatch.context() as patched:
        quiet, without_hint = _run_fraud_transfer(patched, hint=False)
    assert warned and not quiet
    assert with_hint == without_hint
    assert with_hint[1] == "red" and with_hint[6] == 1
