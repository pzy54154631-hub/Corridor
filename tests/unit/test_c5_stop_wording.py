"""C5 (fix step after the B-batch reviews): the stop wording path.

Opus review item 1 and external review item 15 (both 必须): a sentence that negates the
cancellation itself must not stop anything, and a sentence that names a standing transfer
(每月 / 定时 … 转) must not call the interrupt; it is listed for its own revocation card
(list -> yellow card -> device confirm), JUDGING-1 list item 19. C1 closed this for the S1
negation trigger. The lexical stop intent (不转了, 别转了, 停止转账, 取消这笔转账) is a
second way into the same interrupt; these cases reached it on 0c8fed1 and revoked an
established monthly authorization at once, with no card and no device confirmation.

No test calls a model.
"""

import pytest

from corridor.agent import domestic_session
from corridor.agent.domestic_intent import natural_stop, recognize
from corridor.agent.domestic_schedule import cancel_negated, standing_transfer
from corridor.agent.domestic_session import DomesticSession
from corridor.crypto.jcs import H
from corridor.ui.controller import DemoController, demo_state


def _session():
    controller = DemoController(initial_state=demo_state())
    controller.start_session()
    return DomesticSession(controller)


def _with_monthly_authorization():
    session = _session()
    card = session.receive("每月5号给房东转2000元")
    session.confirm(card["response_id"])
    session.verify_mfa(card["response_id"])
    assert session.confirm(card["response_id"])["status"] == "controlled"
    [spa] = [
        item
        for item in session.controller.state.authorizations.values()
        if getattr(item, "authorization_type", None) == "spa"
    ]
    return session, spa


def _runs(session, spa_id):
    return sorted(
        task.status
        for task in session.controller.state.schedules.values()
        if task.spa_ref == spa_id
    )


def _quiet(session):
    controller = session.controller
    state = controller.state
    return (
        controller.gateway.bank.execute_count,
        len(state.user_inputs),
        H(state.schedules),
        H(state.authorizations),
    )


# --- 1. A stop phrase that names a standing transfer lists it for its revocation card ------

NAMES_THE_STANDING_TRANSFER = (
    "每月给房东的转账不转了",
    "每月5号那笔不转了",
    "以后每月都不转了",
    "每月给房东的别转了",
    "定时转账不转了",
    "每月给房东打款的那笔不转了",
)


@pytest.mark.parametrize("text", NAMES_THE_STANDING_TRANSFER)
def test_a_stop_phrase_naming_a_standing_transfer_lists_it_for_revocation(text):
    assert recognize(text).intent == "stop" and standing_transfer(text), text
    session, spa = _with_monthly_authorization()
    before = _quiet(session)
    response = session.receive(text)
    # Nothing is revoked by the sentence itself: the controlled entry lists it.
    assert _runs(session, spa.id) == ["pending", "pending", "pending"], text
    assert _quiet(session) == before, text
    assert "stop_receipt" not in response["payload"], text
    assert response["payload"]["schedule_cancel"]["candidates"] == [spa.id], text
    assert response["intent"] == "clarify" and not response["card"], text
    assert response["buttons"] == [], text
    assert "已有的定时转账授权尚未取消" in response["message"], text


def test_the_listed_authorization_is_then_revoked_only_by_its_card():
    session, spa = _with_monthly_authorization()
    listed = session.receive("每月给房东的转账不转了")
    card = session.prepare_schedule_cancel(listed["response_id"], spa.id)
    assert card["card"]["kind"] == "cancel_schedule"
    assert _runs(session, spa.id) == ["pending", "pending", "pending"]
    assert session.confirm(card["response_id"])["status"] == "controlled"
    assert _runs(session, spa.id) == ["cancelled", "cancelled", "cancelled"]


# --- 2. A stop phrase that negates the cancellation itself stops nothing -------------------

NEGATES_THE_CANCELLATION = (
    "先别取消这笔转账",
    "不要取消转账",
    "别停止转账",
    "不要停止转账",
    "千万别取消这个转账",
)


@pytest.mark.parametrize("text", NEGATES_THE_CANCELLATION)
def test_a_stop_phrase_negating_the_cancellation_interrupts_nothing(text):
    assert recognize(text).intent == "stop" and cancel_negated(text), text
    session, spa = _with_monthly_authorization()
    earlier = session.receive("给妈妈转500元")
    assert earlier["card"]
    before = _quiet(session)
    response = session.receive(text)
    # "Do not cancel" keeps both the open card and the standing authorization.
    assert session.response(earlier["response_id"])["status"] == earlier["status"], text
    assert _runs(session, spa.id) == ["pending", "pending", "pending"], text
    assert _quiet(session) == before, text
    assert "stop_receipt" not in response["payload"], text
    assert not (response["payload"].get("schedule_cancel") or {}).get("candidates"), text
    assert response["intent"] == "clarify" and not response["card"], text
    assert response["message"] == domestic_session._message("negated"), text


# --- 3. A stop phrase inside a sentence keeps an established standing authorization --------

IN_A_SENTENCE = (
    "房东那边先不转了",
    "给房东的那笔先不转了",
    "取消这笔转账",
    "妈妈那边别转了",
)


@pytest.mark.parametrize("text", IN_A_SENTENCE)
def test_a_stop_phrase_in_a_sentence_never_revokes_a_standing_authorization(text):
    assert recognize(text).intent == "stop" and not natural_stop(text), text
    session, spa = _with_monthly_authorization()
    earlier = session.receive("给妈妈转500元")
    assert earlier["card"]
    response = session.receive(text)
    # Like the S1 negation trigger ("先不给房东转了"): the open card is withdrawn ...
    assert session.response(earlier["response_id"])["status"] == "withdrawn", text
    assert response["payload"]["stop_receipt"]["retired_drafts"] == 1, text
    # ... and the established standing authorization is left to its own card.
    assert _runs(session, spa.id) == ["pending", "pending", "pending"], text
    assert response["payload"]["stop_receipt"]["plans"] == [], text
    assert "定时转账授权没有取消" in response["message"], text
    assert session.controller.gateway.bank.execute_count == 0


def test_a_kept_standing_authorization_is_named_even_when_nothing_else_was_open():
    session, spa = _with_monthly_authorization()
    response = session.receive("房东那边先不转了")
    assert _runs(session, spa.id) == ["pending", "pending", "pending"]
    assert response["status"] == "no_active_plan"
    assert response["payload"]["stop_receipt"] == {"plans": [], "retired_drafts": 0}
    assert response["message"].startswith(domestic_session._message("stop_none"))
    assert "定时转账授权没有取消" in response["message"]


# --- 4. A bare stop keeps its S1 meaning; the receipt names only open cards ----------------


@pytest.mark.parametrize("text", ["停止", "不转了"])
def test_a_bare_stop_keeps_its_s1_meaning_for_every_registered_plan(text):
    # Pinned as implemented (proposal PROP-B-DIALOGUE, still open): a bare
    # stop still interrupts every registered plan, the standing authorization included,
    # and its receipt says so.
    assert natural_stop(text), text
    session, spa = _with_monthly_authorization()
    response = session.receive(text)
    assert _runs(session, spa.id) == ["cancelled", "cancelled", "cancelled"], text
    assert "定时转账" in response["message"], text


def test_an_explicit_stop_never_counts_a_finished_card_as_withdrawn():
    session, _ = _with_monthly_authorization()
    response = session.receive("停止")
    # The authorization's own card was confirmed and finished; nothing was withdrawn.
    assert response["payload"]["stop_receipt"]["retired_drafts"] == 0
    assert "撤回" not in response["message"]
