"""session-intent-03: the IM text card omits the recipient and timing facts it asks to confirm.

Stated rules:
- docs/DESIGN-DOMESTIC.md section 3: "审批卡用中文写明：级别、判定理由（规则 ID + 人话解释）、
  金额/收款人/时间等来自可信状态的事实".
- docs/submission-zh/IM接入说明.md: the integration displays message["text"] and
  message["buttons"], and the button click is the device gesture.

channels/im.render_message renders only the banner, a generic prompt, amount, fees and the
tier colour. A red ¥3,200 transfer can be confirmed, MFA-verified and settled through the IM
channel without the text ever naming who receives the money or when.
"""

from corridor.agent.domestic_session import DomesticSession
from corridor.channels.im import IMAdapter
from corridor.i18n import get_locale
from corridor.ui.controller import DemoController

RECIPIENT_WORDS = ("收款", "Recipient", "recipient", "房东", "LANDLORD")


def test_session_intent_03_im_text_names_the_recipient_before_money_moves():
    assert get_locale() == "zh-CN"
    controller = DemoController()
    controller.start_session()
    adapter = IMAdapter(DomesticSession(controller))
    message = adapter.receive("给房东转3200元")
    assert message["card"]["objects"]["contact_ref"] == "CONTACT_LANDLORD"
    assert message["buttons"], "a confirm button is offered"
    text = message["text"]
    assert any(word in text for word in RECIPIENT_WORDS), text


def test_session_intent_03_im_red_transfer_settles_without_recipient_ever_shown():
    controller = DemoController()
    controller.start_session()
    adapter = IMAdapter(DomesticSession(controller))
    message = adapter.receive("给房东转3200元")
    shown = [message["text"]]
    shown.append(adapter.click("confirm", message["response_id"])["text"])
    shown.append(adapter.click("verify_mfa", message["response_id"])["text"])
    final = adapter.click("confirm", message["response_id"])
    assert final["status"] == "settled"
    assert controller.gateway.bank.execute_count == 1
    assert any(any(word in text for word in RECIPIENT_WORDS) for text in shown), shown
