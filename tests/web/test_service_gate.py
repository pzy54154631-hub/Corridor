"""B1: the current-reply rule, the recipient check's establishment and evaluation record,
R7 and the phrase gate, and the words fixed at sending (design §2.5.2, §3.3.2)."""

import pytest

from corridor.ui import scam_friction
from corridor.web import compat, errors
from tests.unit.test_ui_scam_friction import (
    CLARIFY,
    CLARIFY_PLAIN,
    FRAUD,
    NEW,
    SAFE,
    Spy,
    protected,
    snapshot,
)
from tests.web.test_tasks import (
    BIRTHDAY,
    CLARIFY_RED,
    DIRECTED_NEW,
    GUIDED_ANSWERS,
    HELD,
    SCHEDULED_NEW,
    YELLOW,
    Harness,
    inv22,  # noqa: F401  (INV-2.2 probe)
    logged_in,
)


def refused(code, call, *args):
    with pytest.raises(errors.WebError) as caught:
        call(*args)
    assert caught.value.code == code, caught.value.code
    return caught.value


# --- the current reply ------------------------------------------------------------------


def test_choices_answers_and_retries_belong_to_the_current_reply_only(monkeypatch):
    harness = logged_in()
    clarify = harness.send(CLARIFY_PLAIN)
    gated = harness.send(NEW)
    harness.send(YELLOW)
    spy = Spy(harness.controller, monkeypatch)
    before = snapshot(harness.controller, harness.session)
    version = harness.ws.gates["R2"][0]
    refused("not_current", harness.call, "select_recipient", clarify, "c0")
    refused("not_current", harness.call, "gate_answer", gated, version, SAFE)
    refused("not_current", harness.call, "gate_retry", gated, version)
    refused("not_current", harness.call, "choose_birthday", clarify, "c0")
    refused("not_current", harness.call, "complete_aa", clarify, ["c0"])
    assert spy.total() == 0 and snapshot(harness.controller, harness.session) == before
    assert harness.controller.scam_gate_status(harness.identity("T" + gated))["answers"] == {
        "verified": None,
        "guided": None,
    }
    harness.call("make_current", clarify)
    harness.choose(clarify, 1)
    assert harness.step("T" + clarify) == "confirm"
    refused("not_found", harness.call, "make_current", "R99")


def test_a_recipient_choice_locks_once_a_card_is_on_the_device(monkeypatch):
    """§6.5 #23: after the confirmation the reply's recipient is no longer a choice."""
    harness = logged_in()
    rkey = harness.send(CLARIFY_RED)
    harness.choose(rkey, 1)
    tkey = "T" + rkey
    harness.confirm(tkey)
    assert harness.step(tkey) == "mfa_request"
    spy = Spy(harness.controller, monkeypatch)
    before = protected(harness.controller, harness.identity(tkey))
    refused("not_applicable", harness.call, "select_recipient", rkey, "c0")
    assert spy.total() == 0 and protected(harness.controller, harness.identity(tkey)) == before
    assert harness.ws.card_mismatch[rkey] is False
    refused("not_found", harness.call, "make_current", "R0")


def test_a_choice_outside_the_listed_candidates_is_unknown():
    harness = logged_in()
    rkey = harness.send(CLARIFY_PLAIN)
    refused("not_found", harness.call, "select_recipient", rkey, "c9")
    refused("not_found", harness.call, "select_recipient", rkey, "contact")
    yellow = harness.send(YELLOW)
    refused("not_applicable", harness.call, "select_recipient", yellow, "c0")


# --- establishment and the evaluation record --------------------------------------------


def test_the_gate_is_established_for_the_current_reply_with_its_own_words():
    harness = logged_in()
    rkey = harness.send(NEW, files=1)
    identity = harness.identity("T" + rkey)
    status = harness.controller.scam_gate_status(identity)
    assert status["origin"] == {
        "prefix": "domestic",
        "response_id": harness.ws.rkeys[rkey],
        "route": "conversation",
    }
    turn = harness.ws.turn_of(rkey)
    assert turn.hint == compat.vault_tokenize(harness.controller, NEW)
    assert status["context"]["text_hash"] == compat.H(turn.hint)
    assert status["context"]["external_files"] == 1
    assert harness.ws.gate_eval[identity] == {
        "card_identity": scam_friction.cards_identity(
            compat.authoritative_cards(harness.controller, identity)
        ),
        # R1-E6: the record holds for this reply and these words only.
        "context": compat.H({"rid": harness.ws.rkeys[rkey], "text": turn.hint, "files": 1}),
        "result": "gated",
    }
    level0 = harness.send(YELLOW)
    assert harness.ws.gate_eval[harness.identity("T" + level0)]["result"] == "level0"
    assert harness.controller.scam_gate_status(harness.identity("T" + level0)) is None


def test_an_older_reply_is_evaluated_only_when_it_becomes_current_again():
    harness = logged_in()
    old = harness.send(YELLOW)
    identity = harness.identity("T" + old)
    harness.send(NEW)
    harness.ws.gate_eval.pop(identity)
    harness.refresh()
    assert identity not in harness.ws.gate_eval
    assert harness.step("T" + old) == "not_current"
    harness.call("make_current", old)
    assert harness.ws.gate_eval[identity]["result"] == "level0"
    assert harness.step("T" + old) == "confirm"


def test_a_plan_reply_establishes_no_gate_and_needs_no_record():
    harness = logged_in()
    tkey = "T" + harness.send(BIRTHDAY)
    identity = harness.identity(tkey)
    assert harness.controller.scam_gate_status(identity) is None
    assert identity not in harness.ws.gate_eval
    assert "R1" not in harness.ws.gates
    harness.nodes(tkey, ["n0", "n1", "n2", "n3"])
    assert harness.step(tkey) == "confirm_plan"


def test_the_chosen_recipient_is_gated_like_the_envelope(monkeypatch):
    harness = logged_in()
    rkey = harness.send(CLARIFY)
    assert harness.ws.gates == {}
    harness.choose(rkey, 0)
    tkey = "T" + rkey
    identity = harness.identity(tkey)
    status = harness.controller.scam_gate_status(identity)
    assert status is not None and status["level"] >= 1
    assert harness.info(tkey).step == "gate_blocked" and tkey not in harness.ws.gestures
    harness.answer(rkey, SAFE)
    spy = Spy(harness.controller, monkeypatch)
    assert harness.confirm(tkey) == {"status": "settled"}
    assert spy.counts["dispatch"] == 1 and spy.counts["complete_mfa"] == 0


# --- R7 and the phrase gate -------------------------------------------------------------


def test_a_directed_payment_pauses_at_once_and_only_a_retry_clears_the_preset(monkeypatch):
    harness = logged_in()
    rkey = harness.send(DIRECTED_NEW)
    tkey = "T" + rkey
    identity = harness.identity(tkey)
    status = harness.controller.scam_gate_status(identity)
    assert (status["decision"], status["preset"]) == ("pause", {"guided": "yes"})
    assert harness.info(tkey).detail == {"decision": "pause", "challenge": False}
    spy = Spy(harness.controller, monkeypatch)
    rejections = len(harness.controller.scam_gate_rejections())
    refused("not_applicable", harness.answer, rkey, SAFE)
    assert harness.controller.scam_gate_status(identity)["decision"] == "pause"
    assert len(harness.controller.scam_gate_rejections()) == rejections
    harness.retry(rkey)
    status = harness.controller.scam_gate_status(identity)
    assert (status["decision"], status["preset"], status["epoch"]) == ("incomplete", {}, 1)
    harness.answer(rkey, SAFE)
    assert harness.step(tkey) == "confirm"
    assert spy.total() == 0


def test_a_retry_needs_a_paused_gate():
    harness = logged_in()
    rkey = harness.send(FRAUD)
    refused("not_applicable", harness.retry, rkey)
    harness.answer(rkey, {"verified": "yes"})
    assert harness.controller.scam_gate_status(harness.identity("T" + rkey))["decision"] == (
        "incomplete"
    )
    refused("invalid_input", harness.answer, rkey, {"verified": "maybe"})


@pytest.mark.parametrize("source", ["text", "drill"])
def test_the_phrase_gate_shows_no_card_and_holds_no_task(source, monkeypatch):
    harness = logged_in()
    spy = Spy(harness.controller, monkeypatch)
    rkey = harness.send(HELD) if source == "text" else harness.shortcut("scam_drill")
    response = harness.session.response(harness.ws.rkeys[rkey])
    assert response["payload"].get("scam_phrase") and not response["card"]
    assert harness.listed() == [] and harness.ws.gestures == {} and harness.ws.gates == {}
    assert spy.total() == 0 and harness.controller.gateway.bank.execute_count == 0
    if source == "drill":
        assert harness.ws.turn_of(rkey).shortcut == "scam_drill"
        assert harness.ws.turn_of(rkey).hint == compat.tr("polish.scam.drill_utterance")


# --- the words fixed at sending ---------------------------------------------------------


def test_a_language_switch_keeps_the_record_the_words_and_the_token(monkeypatch):
    """§6.5 #17 and #21: switching language or view re-renders text only."""
    harness = logged_in()
    shortcut = harness.shortcut("transfer")
    hint = harness.ws.turn_of(shortcut).hint
    rkey = harness.send(FRAUD)
    tkey = "T" + rkey
    harness.answer(rkey, SAFE)
    identity = harness.identity(tkey)
    record = harness.controller.scam_gate_status(identity)
    held = harness.ws.gestures[tkey]
    gate = harness.ws.gates[rkey]
    before = snapshot(harness.controller, harness.session)
    spy = Spy(harness.controller, monkeypatch)
    harness.call("set_locale", "en")
    harness.call("set_view", "customer")
    harness.call("set_locale", "zh-CN")
    harness.call("set_locale", "en")
    after = harness.controller.scam_gate_status(identity)
    assert (after["epoch"], after["answers"], after["context"]) == (
        record["epoch"],
        record["answers"],
        record["context"],
    )
    assert harness.ws.gestures[tkey] is held and harness.ws.gates[rkey] == gate
    assert harness.ws.turn_of(shortcut).hint == hint
    assert snapshot(harness.controller, harness.session) == before and spy.total() == 0
    assert harness.confirm(tkey) == {"status": "waiting_mfa"}


def test_an_english_shortcut_keeps_its_english_words():
    harness = Harness(locale="en")
    harness.login()
    rkey = harness.shortcut("transfer")
    with harness.bound():
        expected = compat.tr("Transfer 800 yuan to mom")
    assert harness.ws.turn_of(rkey).hint == expected
    harness.call("set_locale", "zh-CN")
    assert harness.ws.turn_of(rkey).hint == expected


def test_a_pause_after_the_code_was_sent_keeps_the_challenge_and_no_code_token(monkeypatch):
    harness = logged_in()
    rkey = harness.send(FRAUD)
    tkey = "T" + rkey
    harness.answer(rkey, SAFE)
    harness.confirm(tkey)
    harness.mfa_request(tkey)
    challenge = compat.pending_entry(harness.controller, harness.identity(tkey))["challenge_id"]
    code = harness.sms_code()
    harness.answer(rkey, GUIDED_ANSWERS)
    info = harness.info(tkey)
    assert (info.step, info.detail) == ("gate_blocked", {"decision": "pause", "challenge": True})
    assert tkey not in harness.ws.gestures
    assert compat.challenge_active(harness.controller, challenge)
    assert harness.sms_code() == code
    spy = Spy(harness.controller, monkeypatch)
    refused("gesture_unknown", harness.call, "device_mfa_verify", tkey, "x" * 22, "0" * 32, code)
    harness.retry(rkey)
    harness.answer(rkey, SAFE)
    assert harness.ws.gestures[tkey].op == "mfa_verify"
    assert harness.mfa_verify(tkey, code) == {"status": "mfa_verified"}
    assert spy.counts["request_mfa"] == 0 and spy.counts["verify"] == 1


def test_a_redisplay_moves_the_task_and_its_inherited_gate_to_the_new_card(monkeypatch):
    """A draft-mode pending redisplayed under a new id: the task follows the new id, the
    inherited gate starts incomplete, and the old token and card version are void."""
    harness = logged_in()
    rkey = harness.send(SCHEDULED_NEW)
    tkey = "T" + rkey
    harness.answer(rkey, SAFE)
    assert harness.step(tkey) == "confirm_intent"
    old_identity = harness.identity(tkey)
    old_token, old_version = harness.token(tkey)
    approve = harness.controller.workflow._approve
    drift = {"left": 1}

    def changed(gateway, prepared, expected, display_id):
        if drift["left"]:
            drift["left"] -= 1
            prepared.proposal.card["amount"] = "301/1"
        return approve(gateway, prepared, expected, display_id)

    monkeypatch.setattr(harness.controller.workflow, "_approve", changed)
    assert harness.confirm(tkey) == {"status": "redisplay"}
    identity = harness.identity(tkey)
    assert identity != old_identity
    assert harness.controller.scam_gate_status(identity)["inherited_from"] == old_identity
    assert harness.info(tkey).detail == {"decision": "incomplete", "challenge": False}
    assert tkey not in harness.ws.gestures
    spy = Spy(harness.controller, monkeypatch)
    refused("gesture_unknown", harness.call, "device_confirm", tkey, old_token, old_version)
    harness.answer(rkey, SAFE)
    assert harness.ws.gestures[tkey].token != old_token
    refused(
        "card_changed", harness.call, "device_confirm", tkey, harness.token(tkey)[0], old_version
    )
    assert spy.total() == 0
    assert harness.confirm(tkey) == {"status": "waiting_mfa"}
