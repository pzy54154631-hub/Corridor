"""session-intent-02: substring alias/pronoun matching silently resolves the wrong recipient.

Stated rule: docs/DESIGN-DOMESTIC.md section 2 "人名解析歧义要追问" and section 5 clarification;
domestic_slots/dialogue only offer *trusted* references, but the lexical match is a raw
substring test (`label in text`, `"他" in text`), so:

- "其他人" / "他们" contain "他" and resolve to the previously confirmed contact;
- "新房东" contains the alias "房东" and resolves to the stored landlord;
- "我妈的朋友" contains the alias "我妈" and resolves to CONTACT_CHEN;
- "Cheng" is tokenized to "NAME_CHENg", which contains the label "NAME_CHEN".

Each produces an executable transfer card for a recipient the user did not name (and in
the first case explicitly excluded), instead of a clarification.
"""

import pytest

from corridor.agent.domestic_session import DomesticSession
from corridor.ui.controller import DemoController


def _session():
    controller = DemoController()
    controller.start_session()
    return DomesticSession(controller)


@pytest.mark.parametrize("text", ["给其他人转100元", "这次给其他人转300元", "给他们每人转100元"])
def test_session_intent_02_other_person_is_not_the_previous_contact(text):
    session = _session()
    first = session.receive("给老妈转200元")
    assert session.confirm(first["response_id"])["status"] == "settled"
    response = session.receive(text)
    recipient = (response["card"].get("objects") or {}).get("contact_ref")
    # Consequence: a single device gesture on the offered card pays the excluded contact.
    if response["card"]:
        session.confirm(response["response_id"])
    paid_to_chen = [
        receipt
        for receipt in session.controller.state.receipts.values()
        if getattr(receipt, "kind", None) == "transfer"
    ]
    assert recipient != "CONTACT_CHEN", (text, response["status"], response["card"].get("amount"))
    assert session.controller.gateway.bank.execute_count == 1, len(paid_to_chen)


@pytest.mark.parametrize(
    "text,wrong",
    [
        ("给新房东转3000元", "CONTACT_LANDLORD"),
        ("给我妈的朋友转500元", "CONTACT_CHEN"),
        # The vault rewrites "Cheng" to "NAME_CHENg"; the label "NAME_CHEN" then matches.
        ("给Cheng转100元", "CONTACT_CHEN"),
    ],
)
def test_session_intent_02_modified_alias_requires_clarification(text, wrong):
    session = _session()
    response = session.receive(text)
    recipient = (response["card"].get("objects") or {}).get("contact_ref")
    assert recipient != wrong, (text, response["status"], response["card"].get("amount"))
    assert response["status"] != "awaiting_input"
