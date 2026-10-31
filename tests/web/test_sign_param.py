"""The simulated device signs the card whose facts the web page showed (§3.5.2, X6).

Driven over HTTP with ``drivers.WebDriver``. Around every device signature the test
records what ``Device.approve`` received and what the device workflow compared it with:
the signed card must be the proposal's card, its business facts (every field but the
draft digest) must equal those of the authoritative card the page was built from, and
the fact rows the page showed for that card must be the ones the fact map states.
"""

from copy import deepcopy

import pytest

from corridor.crypto.jcs import H
from corridor.sim.device_workflow import business_facts
from tests.unit.test_ui_scam_friction import CLARIFY, SAFE
from tests.web.drivers import WebDriver
from tests.web.fact_map import expected_facts, in_view
from tests.web.flows import CARD_LOST, RED, YELLOW


@pytest.fixture
def web():
    driver = WebDriver(view="judge")
    yield driver
    driver.close()


class Signatures:
    """Records each device signature with the proposal and the card it was compared to."""

    def __init__(self, controller):
        self.signed, self.compared = [], []
        approve, workflow = controller.device.approve, controller.workflow
        compare = workflow._approve

        def recording_approve(action, cert, card, **kwargs):
            self.signed.append({"action": H(action), "cert": H(cert), "card": deepcopy(card)})
            return approve(action, cert, card, **kwargs)

        def recording_compare(gateway, prepared, expected, display_id):
            result = compare(gateway, prepared, expected, display_id)
            self.compared.append({
                "expected": deepcopy(expected),
                "proposal": deepcopy(prepared.proposal.card),
                "action": H(prepared.action),
                "status": result.status,
            })
            return result

        controller.device.approve = recording_approve
        workflow._approve = recording_compare


def rows_of(card_vms):
    return [
        [(fact["key"], fact["label"], fact["value"]) for fact in vm["facts"]] for vm in card_vms
    ]


def shown(web):
    """The authoritative cards and the CardVM fact rows both panes show for the reply."""
    ws = web.ws
    task = web._task()
    local = ws.local.get(web.rid) or {}
    payload = ws.session.response(web.rid)["payload"]
    identity = (
        local.get("pending_id")
        or local.get("draft_id")
        or payload.get("pending_id")
        or payload.get("draft_id")
    )
    cards = ws.controller._authoritative_cards(identity)
    chat = rows_of(web.state["chat"]["responses"][web.rkey]["cards"])
    return cards, chat, rows_of(task["cards"]), task


def assert_signed_what_was_shown(web, signatures, cards, rows):
    (signed,) = signatures.signed
    approved = [item for item in signatures.compared if item["status"] == "approved"]
    assert len(approved) == 1
    (compared,) = approved
    assert H(signed["card"]) == H(compared["proposal"])
    assert signed["action"] == compared["action"]
    assert business_facts(compared["expected"]) == business_facts(cards[0])
    assert business_facts(signed["card"]) == business_facts(cards[0])
    with in_view("judge", "zh-CN"):
        assert rows == [expected_facts(cards[0], web.ws.controller)]


def test_yellow_transfer_signs_the_card_both_panes_showed(web):
    signatures = Signatures(web.controller)
    web.send(YELLOW)
    cards, chat, device, task = shown(web)
    assert chat == device and task["step"] == "confirm"
    web.confirm()
    assert_signed_what_was_shown(web, signatures, cards, chat)
    assert web.ws.session.response(web.rid)["status"] == "settled"


def test_red_transfer_signs_only_at_the_final_confirmation(web):
    signatures = Signatures(web.controller)
    web.send(RED)
    web.confirm()
    web.mfa_request()
    web.mfa_verify()
    assert signatures.signed == []
    cards, chat, device, task = shown(web)
    assert chat == device and task["step"] == "final_confirm"
    assert task["hash"]["full"] == H(cards[0]) and task["hash"]["status"] == "pending"
    web.confirm()
    assert_signed_what_was_shown(web, signatures, cards, chat)


def test_clarified_recipient_signs_the_local_card(web):
    signatures = Signatures(web.controller)
    web.send(CLARIFY)
    web.select("CONTACT_LIN_B")
    web.answer(SAFE)
    cards, chat, device, task = shown(web)
    assert chat == device and cards[0]["objects"]["contact_ref"] == "CONTACT_LIN_B"
    web.confirm()
    assert_signed_what_was_shown(web, signatures, cards, chat)


def test_card_operation_signs_its_prepared_display_card(web):
    signatures = Signatures(web.controller)
    web.send(CARD_LOST)
    web.confirm()
    web.mfa_request()
    web.mfa_verify()
    cards, chat, device, task = shown(web)
    assert chat == device
    web.confirm()
    assert_signed_what_was_shown(web, signatures, cards, chat)
