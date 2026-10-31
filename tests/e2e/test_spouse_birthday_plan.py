"""The partner birthday chain follows the selected calendar event end to end (G06/G07)."""

from corridor.agent.domestic_session import DomesticSession
from corridor.channels.im import render_message
from corridor.crypto.jcs import H
from corridor.stores.bank import add_seconds
from corridor.ui.controller import DemoController, demo_state

SPOUSE_DUE = "2026-10-21T16:00:00Z"


def _controller(state=None):
    controller = DemoController(initial_state=state or demo_state(showcase=True))
    controller.start_session()
    return controller


def _confirm_all(controller, draft):
    return controller.confirm_plan(draft["plan_id"], [node["id"] for node in draft["nodes"]])


def _signatures(controller):
    """Inputs, non-login authorizations and bank dispatches; device login is not authority."""
    state = controller.state
    granted = [
        item
        for item in state.authorizations.values()
        if type(item).__name__ != "SessionAuthorization"
    ]
    return len(state.user_inputs), len(granted), controller.gateway.bank.execute_count


def test_preview_binds_goal_spas_and_cards_to_the_selected_event_unsigned():
    controller = _controller()
    draft = controller.preview_birthday(event_ref="BIRTHDAY_SPOUSE")
    assert draft["status"] == "draft"
    assert draft["event"] == {
        "event_ref": "BIRTHDAY_SPOUSE",
        "contact_ref": "CONTACT_SPOUSE",
        "relation": "spouse",
        "event_date": "2026-10-24",
        "order_due": SPOUSE_DUE,
        "lock_month": "2026-10",
        "lock_window_start": "2026-09-30T16:00:00Z",
    }
    plan = controller._plans[draft["plan_id"]]
    requests = controller.workflow._plans[plan["display"].display_id]["requests"]
    goal = next(item for item in requests[0].inputs if item.slot == "goal")
    assert goal.value == {
        "event_ref": "BIRTHDAY_SPOUSE",
        "lock_amount": "1000/1",
        "due_at": SPOUSE_DUE,
    }
    bundle = plan["bundle"]
    assert [(spa.start, spa.end) for spa in bundle["spas"]] == [
        (SPOUSE_DUE, add_seconds(SPOUSE_DUE, 86400))
    ] * 3
    windows = [node["card"]["execution_window"] for node in draft["nodes"]]
    assert windows[0] is None
    assert all(window["start"] == SPOUSE_DUE for window in windows[1:])
    assert _signatures(controller) == (0, 0, 0)


def test_confirmed_partner_plan_executes_at_the_derived_due_time():
    controller = _controller()
    draft = controller.preview_birthday(event_ref="BIRTHDAY_SPOUSE")
    assert _confirm_all(controller, draft)["status"] == "settled"
    assert controller.snapshot()["balances"]["A_LOCKED"] == "1000/1"
    goal = next(item for item in controller.state.user_inputs.values() if item.slot == "goal")
    assert goal.value == {
        "event_ref": "BIRTHDAY_SPOUSE",
        "lock_amount": "1000/1",
        "due_at": SPOUSE_DUE,
    }
    runs = list(controller.state.schedules.values())
    assert len(runs) == 3 and {run.due_at for run in runs} == {SPOUSE_DUE}
    controller.advance_clock(add_seconds(SPOUSE_DUE, -1))
    assert not controller.state.orders
    target = controller.next_due_target()
    assert target == SPOUSE_DUE
    controller.advance_clock(target)
    orders = list(controller.state.orders.values())
    assert sorted(order.merchant_ref for order in orders) == ["CAKE", "FLOWERS"]
    assert all(order.status == "paid" for order in orders)
    assert controller.snapshot()["balances"]["A_LOCKED"] == "500/1"
    assert controller.gateway.verify_log()


def test_prefix_redisplay_keeps_the_same_event_and_dates():
    controller = _controller()
    draft = controller.preview_birthday(event_ref="BIRTHDAY_SPOUSE")
    result = controller.confirm_plan(
        draft["plan_id"], [draft["nodes"][0]["id"], draft["nodes"][1]["id"]]
    )
    assert result["status"] == "redisplay"
    assert result["replacement"]["event"] == draft["event"]
    assert len(result["replacement"]["nodes"]) == 2
    assert result["replacement"]["nodes"][1]["card"]["execution_window"]["start"] == SPOUSE_DUE
    assert _signatures(controller) == (0, 0, 0)


def test_clock_past_order_time_before_confirmation_signs_nothing():
    controller = _controller()
    draft = controller.preview_birthday(event_ref="BIRTHDAY_SPOUSE")
    controller.advance_clock(SPOUSE_DUE)
    controller.start_session()
    result = _confirm_all(controller, draft)
    assert result["status"] == "outside_lock_month"
    assert _signatures(controller) == (0, 0, 0)
    assert not controller.state.orders and not controller.state.schedules


def test_calendar_change_after_preview_requires_redisplay_without_signatures():
    controller = _controller()
    draft = controller.preview_birthday(event_ref="BIRTHDAY_SPOUSE")
    state = controller.state
    moved = state.calendar["BIRTHDAY_SPOUSE"].model_copy(update={"event_date": "2026-10-26"})
    controller.gateway.store.update(calendar={**state.calendar, moved.id: moved})
    result = _confirm_all(controller, draft)
    assert result["status"] == "redisplay"
    assert result["replacement"]["event"]["order_due"] == "2026-10-23T16:00:00Z"
    assert result["replacement"]["plan_id"] != draft["plan_id"]
    assert _signatures(controller) == (0, 0, 0)
    removed = {key: value for key, value in state.calendar.items() if key != "BIRTHDAY_SPOUSE"}
    controller.gateway.store.update(calendar=removed)
    replacement = result["replacement"]
    gone = _confirm_all(controller, replacement)
    assert gone["status"] == "redisplay" and "replacement" not in gone
    assert _signatures(controller) == (0, 0, 0)


def test_preview_before_the_lock_month_is_refused_without_state_change():
    state = demo_state(showcase=True).model_copy(update={"now": "2026-09-20T00:00:00Z"})
    controller = _controller(state)
    before = H(controller.state)
    result = controller.preview_birthday(event_ref="BIRTHDAY_SPOUSE")
    assert result["status"] == "outside_lock_month" and not result["nodes"]
    assert result["event"]["lock_month"] == "2026-10"
    assert H(controller.state) == before


def _session(showcase=True):
    controller = _controller(demo_state(showcase=showcase))
    return DomesticSession(controller)


def test_chat_relation_selects_only_trusted_events_and_ignores_typed_dates():
    session = _session()
    partner = session.receive("我爱人生日快到了")
    assert partner["status"] == "draft"
    assert partner["payload"]["event"]["event_ref"] == "BIRTHDAY_SPOUSE"
    mother = _session().receive("我妈生日快到了")
    assert mother["payload"]["event"]["event_ref"] == "BIRTHDAY"
    assert mother["payload"]["event"]["order_due"] == "2026-10-17T16:00:00Z"
    typed = _session().receive("我爱人12月1日生日帮我安排")
    assert typed["payload"]["event"]["event_ref"] == "BIRTHDAY_SPOUSE"
    assert typed["payload"]["event"]["order_due"] == SPOUSE_DUE
    ambiguous = _session()
    unresolved = ambiguous.receive("帮我安排生日")
    assert unresolved["status"] == "clarify" and unresolved["needs_clarification"]
    assert not unresolved["buttons"] and not unresolved["payload"]["nodes"]
    assert [item["event_ref"] for item in unresolved["payload"]["candidates"]] == [
        "BIRTHDAY",
        "BIRTHDAY_SPOUSE",
    ]
    assert not ambiguous.controller._plans
    assert _signatures(ambiguous.controller) == (0, 0, 0)


def test_baseline_partner_request_clarifies_and_lists_chen_without_inputs():
    session = _session(showcase=False)
    response = session.receive("我爱人生日快到了")
    assert response["status"] == "clarify" and not response["buttons"]
    assert response["payload"]["birthday_status"] == "selection_required"
    assert [item["event_ref"] for item in response["payload"]["candidates"]] == ["BIRTHDAY"]
    assert response["payload"]["candidates"][0]["contact_ref"] == "CONTACT_CHEN"
    assert not session.controller.state.user_inputs
    rendered = render_message(response, controller=session.controller)
    assert not rendered["buttons"] and "BIRTHDAY" in rendered["text"]


def test_chat_father_request_clarifies_instead_of_planning_the_mother_birthday():
    for showcase, expected in ((True, ["BIRTHDAY", "BIRTHDAY_SPOUSE"]), (False, ["BIRTHDAY"])):
        session = _session(showcase=showcase)
        response = session.receive("我爸生日快到了")
        assert response["status"] == "clarify" and not response["buttons"]
        assert response["payload"]["birthday_status"] == "selection_required"
        assert [item["event_ref"] for item in response["payload"]["candidates"]] == expected
        assert not session.controller._plans
        assert _signatures(session.controller) == (0, 0, 0)


def test_banner_names_only_the_partner_birthday_and_previews_that_event():
    from tests.e2e.test_demo_ui import app

    page = app()
    page.button(key="enter").click().run()
    captions = [item.value for item in page.caption]
    assert any("2026-10-22 00:00:00" in value for value in captions)
    assert not any("2026-10-18 00:00:00" in value for value in captions)
    page.button(key="birthday_suggestion").click().run()
    assert not page.exception
    assert page.session_state["birthday_draft"]["event"]["event_ref"] == "BIRTHDAY_SPOUSE"
    assert page.session_state["active_page"] == "birthday"


def test_chat_candidate_button_previews_only_the_chosen_event():
    from tests.e2e.test_demo_ui import app

    page = app()
    page.button(key="enter").click().run()
    page.text_input(key="domestic_text").set_value("帮我安排生日")
    page.button(key="domestic_send").click().run()
    assert not page.exception
    session = page.session_state["domestic_session"]
    response = session.response(page.session_state["domestic_response_id"])
    key = f"domestic_birthday:{response['response_id']}:BIRTHDAY_SPOUSE"
    page.button(key=key).click().run()
    assert not page.exception
    assert page.session_state["birthday_draft"]["event"]["event_ref"] == "BIRTHDAY_SPOUSE"
    assert page.session_state["active_page"] == "birthday"
    assert not session.controller.state.user_inputs
    assert session.controller.snapshot()["dispatch_count"] == 0


def test_task_plan_button_keeps_the_event_selected_in_chat():
    from tests.e2e.test_demo_ui import app, navigate

    page = app()
    page.button(key="enter").click().run()
    page.text_input(key="domestic_text").set_value("我妈生日快到了")
    page.button(key="domestic_send").click().run()
    assert page.session_state["birthday_draft"]["event"]["event_ref"] == "BIRTHDAY"
    navigate(page, "dag")
    page.button(key="preview_birthday").click().run()
    assert not page.exception
    # The mother's plan chosen in chat is never swapped for the partner's default.
    assert page.session_state["birthday_draft"]["event"]["event_ref"] == "BIRTHDAY"
    controller = page.session_state["controller"]
    assert not controller.state.user_inputs
    assert controller.snapshot()["dispatch_count"] == 0
