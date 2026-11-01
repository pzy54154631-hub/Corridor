"""C53: withdrawing pending work must preserve prior advice and stop receipts."""

from copy import deepcopy

import pytest

from tests.web.test_viewmodels import Web


@pytest.mark.parametrize("message", [None, "你好"])
def test_cancel_preserves_a_clarification_that_proposed_no_action(message):
    web = Web().login()
    key = web.shortcut("scam_drill") if message is None else web.send(message)
    identifier = web.ws.rkeys[key]
    before = deepcopy(web.ws.session.response(identifier))
    assert before["intent"] == "clarify"
    if message is None:
        assert "96110" in before["message"]
    payment = web.send("给妈妈转100元")
    web.post("device_cancel", web.task(payment)["key"])
    assert web.ws.session.response(identifier) == before
    assert web.ws.session.response(web.ws.rkeys[payment])["status"] == "withdrawn"
    assert web.controller.gateway.bank.execute_count == 0


@pytest.mark.parametrize("pending", [False, True])
def test_later_cancel_preserves_the_previous_stop_receipt(pending):
    web = Web().login()
    if pending:
        web.send("给妈妈转100元")
    stopped = web.send("停止")
    identifier = web.ws.rkeys[stopped]
    before = deepcopy(web.ws.session.response(identifier))
    assert before["status"] in {"interrupted", "no_active_plan"}
    payment = web.send("给妈妈转100元")
    web.post("device_cancel", web.task(payment)["key"])
    assert web.ws.session.response(identifier) == before


def test_stop_still_withdraws_an_unselected_transfer():
    web = Web().login()
    choice = web.send("给Lin转100元")
    identifier = web.ws.rkeys[choice]
    assert web.ws.session.response(identifier)["intent"] == "transfer"
    web.post("stop_all")
    response = web.ws.session.response(identifier)
    assert response["status"] == "withdrawn" and response["buttons"] == []


def test_business_cancel_uses_the_cancel_marker_without_an_empty_nodes_heading():
    web = Web().login()
    web.post("subscriptions_query")
    task = web.state()["device"]["tasks"][0]
    web.post("device_cancel", task["key"])
    assert web.ws.transcript[-1].marker == "cancel"
    turn = web.state()["chat"]["turns"][-1]
    assert "取消" in turn["text"]
    assert "全部停止" not in turn["text"]
    assert "逐节点执行结果" not in turn["text"]
