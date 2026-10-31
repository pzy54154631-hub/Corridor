"""session-intent-01: unrecognised scheduling phrases fall through to an immediate transfer.

Stated rules:
- agent/domestic_slots.has_date_request docstring: "An unparsed date is unresolved
  scheduling intent, never an immediate transfer."
- Scheduling rule: "无效或冲突日期不得退化为即时转账".
- docs/DESIGN-DOMESTIC.md section 2: scheduled transfers enter the task DAG; section 3: the
  approval card must state amount/recipient/time facts.

Common synonyms of the design's own demo utterance "下周五再给房东转三千二" (e.g. 下星期五,
下礼拜五) and other everyday time expressions are not recognised, so the session previews an
immediate transfer (execution_window=None) that settles at the current simulated time.
"""

import pytest

from corridor.agent.domestic_session import DomesticSession
from corridor.ui.controller import DemoController


def _session():
    controller = DemoController()
    controller.start_session()
    return DomesticSession(controller)


SCHEDULED = [
    "下星期五给房东转三千二",
    "下礼拜五给房东转三千二",
    "星期五给房东转三千二",
    "三天后给房东转三千二",
    "过两天给房东转三千二",
    "今晚给房东转三千二",
    "一周后给房东转三千二",
    "后日给房东转3200元",
    "下个星期给房东转3200元",
]


@pytest.mark.parametrize("text", SCHEDULED)
def test_session_intent_01_scheduling_phrase_never_becomes_immediate_card(text):
    session = _session()
    response = session.receive(text)
    # The control case "下周五给房东转三千二" yields awaiting_device_selection and no card.
    assert not response["card"], (text, response["status"], response["card"].get("amount"))
    assert response["needs_clarification"]
    assert session.controller.gateway.bank.execute_count == 0


def test_session_intent_01_scheduled_rent_is_not_paid_now_after_card_flow():
    session = _session()
    now = session.controller.state.now
    response = session.receive("下星期五给房东转三千二")
    identifier = response["response_id"]
    # Walk the documented red-tier gestures: confirm -> mfa -> confirm.
    session.confirm(identifier)
    session.verify_mfa(identifier)
    final = session.confirm(identifier)
    assert session.controller.state.now == now
    # A next-Friday instruction must not settle a transfer at the current simulated time.
    assert session.controller.gateway.bank.execute_count == 0, final["status"]
