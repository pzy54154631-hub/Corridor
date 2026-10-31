"""Contest UI controls preserve the frozen device and gateway protocols."""

from corridor.crypto.jcs import H
from corridor.ui.controller import DemoController, demo_state


def test_seeded_card_and_mature_position_are_owned_and_redeemable():
    state = demo_state(showcase=True)
    assert state.cards["CARD_UI_DEMO"].owner == "C_DEMO"
    position = state.positions["POSITION_UI_DEMO"]
    assert position.owner == "C_DEMO" and position.redeemable_at <= state.now
    controller = DemoController(initial_state=state)
    controller.start_session()
    pending = controller.redeem(position.id)
    assert pending["card"]["tier"] == "yellow"
    assert controller.approve_pending(pending["pending_id"])["status"] == "settled"


def test_showcase_data_does_not_change_default_frozen_runner_state():
    baseline = demo_state()
    controller = DemoController()
    assert not baseline.cards and not baseline.positions
    assert H(controller.state) == H(baseline)
    showcase = demo_state(showcase=True)
    assert set(showcase.cards) == {"CARD_UI_DEMO"}
    assert set(showcase.positions) == {"POSITION_UI_DEMO"}
    # G06/G07: showcase also adds a synthetic partner contact and birthday, only as new objects.
    assert set(showcase.contacts) - set(baseline.contacts) == {"CONTACT_SPOUSE"}
    assert set(showcase.calendar) - set(baseline.calendar) == {"BIRTHDAY_SPOUSE"}
    assert all(showcase.contacts[key] == value for key, value in baseline.contacts.items())
    assert all(showcase.calendar[key] == value for key, value in baseline.calendar.items())
    stripped = showcase.model_copy(
        update={
            "cards": {},
            "positions": {},
            "contacts": {
                key: value for key, value in showcase.contacts.items() if key != "CONTACT_SPOUSE"
            },
            "calendar": {
                key: value for key, value in showcase.calendar.items() if key != "BIRTHDAY_SPOUSE"
            },
        }
    )
    assert H(stripped) == H(baseline)


def test_actual_app_seeds_showcase_data_once_without_authority():
    from tests.e2e.test_demo_ui import app

    page = app()
    controller = page.session_state["controller"]
    assert set(controller.state.cards) == {"CARD_UI_DEMO"}
    assert set(controller.state.positions) == {"POSITION_UI_DEMO"}
    assert not controller.state.authorizations
    assert not controller.state.user_inputs
    before = H(controller.state)
    page.run()
    assert page.session_state["controller"] is controller
    assert H(controller.state) == before


def test_password_control_requires_manual_mfa_and_never_calls_bank():
    controller = DemoController()
    controller.start_session()
    state_hash = H(controller.state)
    pending = controller.preview_password_change(request_id="ui-password")
    assert H(controller.state) == state_hash
    assert pending["card"]["tier"] == "red"
    identifier = pending["pending_id"]
    assert controller.approve_pending(identifier)["status"] == "waiting_mfa"
    assert controller.state.customers["C_DEMO"].credential_version == 1
    challenge = controller.request_mfa(identifier)["challenge_id"]
    code = controller.gateway.mfa.test_code(challenge)
    wrong = "000000" if code != "000000" else "111111"
    assert controller.complete_mfa(identifier, code=wrong)["status"] == "rejected"
    assert controller.approve_pending(identifier)["status"] == "waiting_mfa"
    assert controller.complete_mfa(identifier, code=code)["status"] == "mfa_verified"
    assert controller.approve_pending(identifier)["status"] == "controlled"
    assert controller.state.customers["C_DEMO"].credential_version == 2
    assert controller.approve_pending(identifier)["status"] == "controlled"
    assert controller.state.customers["C_DEMO"].credential_version == 2
    assert controller.gateway.bank.execute_count == 0
    assert controller.gateway.verify_log()
    assert all("code" not in row for row in controller.gateway.audit.records())


def test_manual_wrong_codes_preserve_verifier_three_attempt_lock():
    controller = DemoController()
    controller.start_session()
    pending = controller.preview_password_change()["pending_id"]
    controller.approve_pending(pending)
    challenge = controller.request_mfa(pending)["challenge_id"]
    code = controller.gateway.mfa.test_code(challenge)
    for _ in range(3):
        controller.complete_mfa(pending, code="000000" if code != "000000" else "111111")
    assert controller.gateway.safety_snapshot("C_DEMO")["state"] == "Locked"
    assert controller.complete_mfa(pending, code=code)["status"] == "rejected"
    assert controller.state.customers["C_DEMO"].credential_version == 1


def test_birthday_banner_is_readonly_and_opens_unsigned_preview():
    from tests.e2e.test_demo_ui import app

    page = app()
    page.button(key="enter").click().run()
    controller = page.session_state["controller"]
    before = H(controller.state)
    page.run()
    assert H(controller.state) == before
    page.button(key="birthday_suggestion").click().run()
    assert not page.exception
    assert page.session_state["active_page"] == "birthday"
    assert page.selectbox(key="page").value == "birthday"
    page.run()
    assert page.button(key="birthday_advance_600")
    assert page.session_state["birthday_draft"]["nodes"]
    assert H(controller.state) == before


def test_birthday_route_has_no_duplicate_state_warning_and_counts_selected_nodes():
    from tests.e2e.test_demo_ui import app

    page = app()
    page.button(key="enter").click().run()
    # G06: 2026-10-18 was Chen/mom's order time shown as the partner's; 10-22 is the partner's.
    assert any("2026-10-22 00:00:00" in item.value for item in page.caption)
    assert not any("2026-10-18 00:00:00" in item.value for item in page.caption)
    page.button(key="birthday_suggestion").click().run()
    assert not page.exception
    assert not any("Session State API" in item.value for item in page.warning)
    page.button(key="chain_probe_subscription").click().run()
    assert page.selectbox(key="page").value == "birthday"
    assert page.session_state["active_page"] == "birthday"
    assert not any("Session State API" in item.value for item in page.warning)
    draft = page.session_state["birthday_draft"]
    selected = f"plan_select:{draft['plan_id']}:{draft['nodes'][0]['id']}"
    total = len(draft["nodes"])
    # "0 needed" read as no approval at all; the count now shows plan items and selections.
    assert next(item.value for item in page.metric if item.label == "本链需逐项审批") == (
        f"共 {total} 项 · 已勾选 0"
    )
    page.checkbox(key=selected).check().run()
    # Same metric after one selection: the selected count is still asserted.
    assert next(item.value for item in page.metric if item.label == "本链需逐项审批") == (
        f"共 {total} 项 · 已勾选 1"
    )


def test_stop_retires_business_previews_and_read_results():
    from tests.e2e.test_demo_ui import app

    page = app()
    page.button(key="enter").click().run()
    retired = (
        "aa_pending",
        "aa_result",
        "transaction_query_result",
        "income_query_result",
        "password_pending",
    )
    for key in retired:
        page.session_state[key] = {"stale": True}
    page.button(key="stop_all").click().run()
    assert not page.exception
    assert all(key not in page.session_state.filtered_state for key in retired)


def test_wealth_manual_code_uses_real_verifier_without_automatic_device_read(monkeypatch):
    from tests.unit.test_ui_wealth_controller import prepared_controller

    controller = prepared_controller()
    overview = controller.prepare_allocation()
    controller.start_allocation([item["item_ref"] for item in overview["items"]])
    identifier = controller.leg["action"].action_id
    challenge = controller.request_leg_mfa(identifier)
    code = controller.gateway.mfa.test_code(challenge)
    monkeypatch.setattr(controller.device, "complete_mfa", lambda *a, **kw: None)
    assert controller.verify_leg(identifier, code="incorrect")["status"] == "rejected"
    assert controller.approve_leg(identifier)["status"] == "waiting_mfa"
    assert controller.verify_leg(identifier, code=code)["status"] == "mfa_verified"
    assert controller.approve_leg(identifier)["status"] == "settled"


def test_password_page_requires_typed_code_and_clears_transient_widget():
    from tests.e2e.test_demo_ui import app, navigate

    page = app()
    page.button(key="enter").click().run()
    navigate(page, "device")
    page.button(key="preview_password").click().run()
    controller = page.session_state["controller"]
    pending = page.session_state["password_pending"]["pending_id"]
    page.button(key=f"approve:{pending}").click().run()
    prefix = f"device_mfa:{pending}"
    page.button(key=prefix + ":request").click().run()
    page.button(key=prefix + ":verify").click().run()
    assert not controller._pending[pending]["verified"]
    challenge = controller._pending[pending]["challenge_id"]
    code = controller.gateway.mfa.test_code(challenge)
    page.text_input(key=prefix + ":code").set_value(code)
    page.button(key=prefix + ":verify").click().run()
    assert page.session_state[prefix + ":code"] == ""
    page.button(key=f"approve:{pending}").click().run()
    assert not page.exception
    assert controller.state.customers["C_DEMO"].credential_version == 2
    assert controller.gateway.bank.execute_count == 0


def test_safety_reason_discloses_benign_changes():
    from corridor.i18n import reset_locale, set_locale
    from corridor.ui.messages import UI_MESSAGES
    from corridor.ui.safety import safety_projection

    locale = set_locale("zh-CN")
    try:
        view = safety_projection({"state": "Locked", "reason": "kernel_rejections"})
        # The lock has an account-level cumulative path and a session-level path; the old
        # "consecutive" copy described only the latter (UI-POLISH P1).
        assert view["reasons"] == [UI_MESSAGES["polish.safety.reason.kernel_rejections"]["zh-CN"]]
    finally:
        reset_locale(locale)
