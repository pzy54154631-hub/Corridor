import pytest

from corridor.web import contracts, service
from tests.unit.test_ui_scam_friction import Spy
from tests.web.test_api_chat import Browser


def choose_schedule(page, rkey, *, choice="c0", version=None, expect=200):
    shown = page.response(rkey)["schedule_cancel_select"]
    return page.post(
        f"/api/chat/responses/{rkey}/schedule-cancel",
        {"choice": choice, "list_version": version or shown["list_version"]},
        expect=expect,
    )


def _create_schedule(page):
    rkey = page.send("每月5号给房东转500元")
    assert page.ws.session.response(page.ws.rkeys[rkey])["card"]["kind"] == "schedule_transfer"
    page.red("T" + rkey)
    assert page.response(rkey)["status_text"] == "已建立定时授权"


@pytest.mark.parametrize("view", ["customer", "judge"])
def test_schedule_cancel_candidates_are_rendered_as_an_explicit_choice(web_app, view):
    page = Browser(web_app, view=view)
    page.login()
    _create_schedule(page)
    listed = page.send("取消每月5号给房东的转账")
    vm = page.response(listed)
    assert vm["status"] == "clarify" and vm["cards"] == []
    assert vm["schedule_cancel_select"]["choices"]


@pytest.mark.parametrize("view", ["customer", "judge"])
def test_choice_prepares_only_the_native_card_and_device_confirmation_revokes(
    web_app, monkeypatch, view
):
    page = Browser(web_app, view=view)
    page.login()
    _create_schedule(page)
    (spa,) = [
        item.id
        for item in page.ws.controller.state.authorizations.values()
        if item.authorization_type == "spa"
    ]
    listed = page.send("取消每月5号给房东的转账")
    shown = page.response(listed)["schedule_cancel_select"]
    runs_before = {
        item.id: item.model_dump(mode="json")
        for item in page.ws.controller.state.schedules.values()
        if item.spa_ref == spa
    }
    bank_before = page.ws.controller.state
    auth_before = {
        key: item.model_dump(mode="json") for key, item in bank_before.authorizations.items()
    }
    ledger_before = [item.model_dump(mode="json") for item in bank_before.ledger]
    original = page.ws.session.response(page.ws.rkeys[listed])
    assert original["payload"]["schedule_cancel"]["candidates"] == [spa]
    assert page.response(listed)["device_task"] is None
    spy = Spy(page.ws.controller, monkeypatch)
    assert (
        service.schedule_cancel_choices(page.ws, page.ws.rkeys[listed])[1] == shown["list_version"]
    )

    result = choose_schedule(page, listed)
    contracts.validate_response(result, view=view)
    chosen = result["rkey"]
    prepared = page.ws.session.response(page.ws.rkeys[chosen])
    assert chosen != listed and page.state["chat"]["current"] == chosen
    assert page.ws.session.response(page.ws.rkeys[listed]) == original
    assert prepared["card"]["kind"] == "cancel_schedule"
    assert prepared["card"]["objects"]["schedule_ref"] == spa
    assert prepared["status"] == "awaiting_approval"
    assert spy.counts["approve"] == spy.counts["dispatch"] == 0
    assert {
        item.id: item.model_dump(mode="json")
        for item in page.ws.controller.state.schedules.values()
        if item.spa_ref == spa
    } == runs_before
    bank_after = page.ws.controller.state
    assert {
        key: item.model_dump(mode="json") for key, item in bank_after.authorizations.items()
    } == auth_before
    assert [item.model_dump(mode="json") for item in bank_after.ledger] == ledger_before
    assert shown["locked"] is False

    page.device("T" + chosen, "confirm")
    assert page.ws.session.response(page.ws.rkeys[chosen])["status"] == "controlled"
    assert page.response(chosen)["status_text"] == "已撤销定时授权"
    done_text = "已撤销这项定时转账授权的剩余执行；已经执行的转账不会撤销。"
    assert page.response(chosen)["result"]["banner"]["text"] == done_text
    assert page.task("T" + chosen)["result"]["banner"]["text"] == done_text
    assert all(
        item.status == "cancelled"
        for item in page.ws.controller.state.schedules.values()
        if item.spa_ref == spa
    )
    assert spy.counts["approve"] == spy.counts["dispatch"] == 1


def test_schedule_choice_refuses_stale_facts_wrong_object_and_replayed_reply(web_app, monkeypatch):
    page = Browser(web_app)
    page.login()
    _create_schedule(page)
    listed = page.send("取消每月5号给房东的转账")
    shown = page.response(listed)["schedule_cancel_select"]
    refs = page.ws.session.response(page.ws.rkeys[listed])["payload"]["schedule_cancel"][
        "candidates"
    ]
    spa = page.ws.controller.state.authorizations[refs[0]]
    runs = [
        item
        for item in page.ws.controller.state.schedules.values()
        if item.spa_ref == spa.id and item.status in {"pending", "due", "waiting_mfa"}
    ]
    run = runs[0]
    page.ws.controller.gateway.store.update(
        schedules={
            **page.ws.controller.state.schedules,
            run.id: run.model_copy(update={"status": "cancelled"}),
        }
    )
    spy = Spy(page.ws.controller, monkeypatch)
    result = choose_schedule(page, listed, version=shown["list_version"], expect=409)
    assert result["error"]["code"] == "card_changed"
    assert spy.total() == 0
    assert page.ws.controller.state.authorizations[spa.id] == spa


@pytest.mark.parametrize("choice, expected", [("c99", "not_found")])
def test_schedule_choice_rejects_an_unlisted_object_without_effects(
    web_app, monkeypatch, choice, expected
):
    page = Browser(web_app)
    page.login()
    _create_schedule(page)
    listed = page.send("取消每月5号给房东的转账")
    spy = Spy(page.ws.controller, monkeypatch)
    result = choose_schedule(page, listed, choice=choice, expect=404)
    assert result["error"]["code"] == expected
    assert spy.total() == 0


def test_schedule_choice_requires_current_reply_and_cannot_be_replayed(web_app, monkeypatch):
    page = Browser(web_app)
    page.login()
    _create_schedule(page)
    listed = page.send("取消每月5号给房东的转账")
    version = page.response(listed)["schedule_cancel_select"]["list_version"]
    spy = Spy(page.ws.controller, monkeypatch)
    result = choose_schedule(page, listed)
    chosen = result["rkey"]
    assert page.ws.session.response(page.ws.rkeys[chosen])["card"]["kind"] == "cancel_schedule"
    assert (
        choose_schedule(page, listed, version=version, expect=409)["error"]["code"] == "not_current"
    )
    replay = page.post(
        f"/api/chat/responses/{chosen}/schedule-cancel",
        {"choice": "c0", "list_version": version},
        expect=409,
    )
    assert replay["error"]["code"] in {"card_changed", "not_applicable"}
    assert spy.total() == 0


def test_two_scheduled_authorizations_keep_the_selected_object_binding(web_app, monkeypatch):
    page = Browser(web_app)
    page.login()
    _create_schedule(page)
    second = page.send("每月10号给妈妈转600元")
    assert page.ws.session.response(page.ws.rkeys[second])["card"]["kind"] == "schedule_transfer"
    page.red("T" + second)
    listed = page.send("取消定时转账")
    shown = page.response(listed)["schedule_cancel_select"]
    refs = page.ws.session.response(page.ws.rkeys[listed])["payload"]["schedule_cancel"][
        "candidates"
    ]
    assert len(refs) == len(shown["choices"]) == 2
    spy = Spy(page.ws.controller, monkeypatch)

    result = choose_schedule(page, listed, choice="c1")
    chosen = page.ws.session.response(page.ws.rkeys[result["rkey"]])
    assert chosen["card"]["kind"] == "cancel_schedule"
    assert chosen["card"]["objects"]["schedule_ref"] == refs[1]
    assert all(
        task.status != "cancelled"
        for task in page.ws.controller.state.schedules.values()
        if task.spa_ref in refs
    )
    assert spy.counts["approve"] == spy.counts["dispatch"] == 0
