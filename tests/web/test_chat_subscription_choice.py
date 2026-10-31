"""A chat cancellation names one live, displayed authorization before review."""

from copy import deepcopy

import pytest

from corridor.web import contracts
from tests.unit.test_ui_scam_friction import Spy
from tests.web.test_api_chat import Browser


def choices(page, rkey):
    return page.response(rkey)["subscription_select"]


def choose(page, rkey, *, choice="c0", version=None, expect=200):
    return page.post(
        f"/api/chat/responses/{rkey}/subscription",
        {
            "choice": choice,
            "list_version": version or choices(page, rkey)["list_version"],
        },
        expect=expect,
    )


@pytest.mark.parametrize("view", ["customer", "judge"])
@pytest.mark.parametrize("locale", ["zh-CN", "en"])
def test_explicit_choice_prepares_a_new_card_and_only_device_confirms(
    web_app, monkeypatch, view, locale
):
    page = Browser(web_app, view=view, locale=locale)
    page.login()
    spy = Spy(page.ws.controller, monkeypatch)
    rkey = page.send("取消订阅")
    shown = choices(page, rkey)
    original = deepcopy(page.ws.session.response(page.ws.rkeys[rkey]))
    assert page.response(rkey)["status"] == "clarify" and page.response(rkey)["cards"] == []
    assert page.response(rkey)["device_task"] is None and spy.total() == 0
    assert shown["choices"] and shown["locked"] is False
    assert len({row["label"] for row in shown["choices"]}) == len(shown["choices"])
    ref = original["payload"]["subscription_choices"][0]
    result = choose(page, rkey)
    contracts.validate_response(result, view=view)
    new = result["rkey"]
    assert new != rkey and page.state["chat"]["current"] == new
    assert page.ws.session.response(page.ws.rkeys[rkey]) == original
    assert choices(page, rkey)["locked"] is True
    prepared = page.ws.session.response(page.ws.rkeys[new])
    assert prepared["card"]["objects"]["authorization_ref"] == ref
    assert prepared["status"] == "awaiting_approval"
    assert spy.counts["approve"] == spy.counts["dispatch"] == 0
    assert page.ws.controller.state.subscriptions[ref].status == "active"
    page.device("T" + new, "confirm")
    assert page.ws.controller.state.subscriptions[ref].status == "cancelled"
    assert spy.counts["approve"] == spy.counts["dispatch"] == 1


def test_stale_choices_never_rebind_a_changed_or_removed_object(web_app, monkeypatch):
    page = Browser(web_app)
    page.login()
    rkey = page.send("取消订阅")
    shown = choices(page, rkey)
    refs = page.ws.session.response(page.ws.rkeys[rkey])["payload"]["subscription_choices"]
    c = page.ws.controller
    original = c.state.subscriptions[refs[0]]
    c.gateway.store.update(
        subscriptions={
            **c.state.subscriptions,
            refs[0]: original.model_copy(update={"amount": "51/1"}),
        }
    )
    before = deepcopy(c.snapshot())
    spy = Spy(c, monkeypatch)
    result = choose(page, rkey, version=shown["list_version"], expect=409)
    assert result["error"]["code"] == "card_changed"
    assert c.snapshot() == before and spy.total() == 0


def test_choice_requires_current_reply_and_this_session_version(web_app, monkeypatch):
    first, second = Browser(web_app), Browser(web_app)
    for page in (first, second):
        page.login()
    rkey, other = first.send("取消订阅"), second.send("取消订阅")
    version = choices(first, rkey)["list_version"]
    spy = Spy(second.ws.controller, monkeypatch)
    assert choose(second, other, version=version, expect=409)["error"]["code"] == "card_changed"
    assert choose(second, other, choice="c99", expect=404)["error"]["code"] == "not_found"
    own = choices(second, other)["list_version"]
    second.send("取消订阅")
    assert choose(second, other, version=own, expect=409)["error"]["code"] == "not_current"
    assert spy.total() == 0


def test_projection_and_choice_exclude_other_owners_and_retired_items(web_app):
    page = Browser(web_app)
    page.login()
    c = page.ws.controller
    own = next(iter(c.state.subscriptions.values()))
    c.gateway.store.update(
        subscriptions={
            **c.state.subscriptions,
            "FOREIGN": own.model_copy(update={"id": "FOREIGN", "owner": "OTHER"}),
            "RETIRED": own.model_copy(update={"id": "RETIRED", "status": "cancelled"}),
        }
    )
    rkey = page.send("取消订阅")
    refs = page.ws.session.response(page.ws.rkeys[rkey])["payload"]["subscription_choices"]
    assert "FOREIGN" not in refs and "RETIRED" not in refs
    assert len(choices(page, rkey)["choices"]) == len(refs)
    before = deepcopy((c.snapshot(), page.ws.gestures, page.ws.session.history))
    page.get("/api/state")
    assert (c.snapshot(), page.ws.gestures, page.ws.session.history) == before
