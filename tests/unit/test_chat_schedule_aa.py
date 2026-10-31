"""Chat yields the business forms' own scheduled-transfer and named-AA cards (G11)."""

from types import SimpleNamespace

import pytest

from corridor.agent.domestic_schedule import aa_candidate, schedule_candidate
from corridor.agent.domestic_session import DomesticSession
from corridor.agent.domestic_slots import trusted_slot_context
from corridor.channels.im import render_message
from corridor.crypto.jcs import H
from corridor.kernel.spa import due_at
from corridor.ui import business_views, operations, structured_transfer
from corridor.ui.controller import DemoController

NOW = "2026-10-16T02:00:00Z"


def _controller():
    controller = DemoController()
    controller.start_session()
    return controller


def _session(controller=None):
    return DomesticSession(controller or _controller())


def _aa_context(controller, text):
    return {
        **trusted_slot_context(controller.state, controller.device.principal),
        "contact_text": controller.gateway.vault.tokenize(text),
    }


@pytest.mark.parametrize(
    "text,kind,date_text,monthly_day",
    [
        ("每月15号给妈妈转500", "monthly", "2026-11-15", 15),
        ("每个月十五日给妈妈转500", "monthly", "2026-11-15", 15),
        ("每月20号给妈妈转500", "monthly", "2026-10-20", 20),
        ("明天给妈妈转50", "once", "2026-10-17", None),
        ("下周五给房东转三千二", "once", "2026-10-23", None),
        ("月底给房东转3200元", "once", "2026-10-31", None),
        ("2026-10-20给妈妈转200", "once", "2026-10-20", None),
        ("10月20号给妈妈转200", "once", "2026-10-20", None),
        ("当月20号给妈妈转200", "once", "2026-10-20", None),
        ("20号给妈妈转200", "once", "2026-10-20", None),
        ("给妈妈转200，明天", "once", "2026-10-17", None),
    ],
)
def test_schedule_grammar_positive(text, kind, date_text, monthly_day):
    result = schedule_candidate(text, {"now": NOW})
    assert result["status"] == "complete", result
    assert (result["kind"], result["date_text"], result["monthly_day"]) == (
        kind,
        date_text,
        monthly_day,
    )


def test_schedule_grammar_follows_the_beijing_business_day():
    context = {"now": "2026-10-31T16:00:00Z"}
    expected = {
        "明天": "2026-11-02",
        "月底": "2026-11-30",
        "下周五": "2026-11-06",
        "每月15号": "2026-11-15",
    }
    for phrase, date_text in expected.items():
        assert schedule_candidate(phrase + "给妈妈转50", context)["date_text"] == date_text


@pytest.mark.parametrize(
    "text",
    [
        "每月31号给妈妈转500",
        "每月29号给妈妈转500",
        "15号给妈妈转500",
        "16号给妈妈转500",
        "10月1号给妈妈转500",
        "明天和下周五给妈妈转50",
        "明天下午3点给妈妈转50",
        "每月15号给妈妈转500持续一年",
        "每月15号给妈妈转500，持续一年",
        "定时给Chen转100元",
        "生日前两天给房东转三千二",
        "下星期五给房东转三千二",
        "下礼拜五给房东转三千二",
        "下下周五给房东转3200",
        "二十日后给房东转3200",
        "给20号楼房东转3200",
        "每月给妈妈转500",
        "每周五给妈妈转500",
        "2026-02-30给妈妈转500",
    ],
)
def test_schedule_grammar_negative_is_unresolved(text):
    assert schedule_candidate(text, {"now": NOW})["status"] == "unresolved"


def test_birthday_template_is_left_to_the_birthday_chain():
    result = schedule_candidate("生日前两天给房东转三千二", {"now": NOW})
    assert result["reason"] == "birthday_template"
    assert schedule_candidate("给妈妈转500", {"now": NOW}) == {"status": "none"}


def test_named_aa_participants_and_exact_collection():
    controller = _controller()
    text = "我和Chen、同学AA这顿饭300元"
    result = aa_candidate(text, _aa_context(controller, text))
    assert result["status"] == "complete"
    assert result["participants"] == ["CONTACT_CHEN", "CONTACT_LIN_A"]
    assert (result["people"], result["total"], result["per_person"]) == (3, "300/1", "100/1")
    assert result["collect_total"] == "200/1"
    reverse = "Chen和我AA这顿饭200元"
    assert aa_candidate(reverse, _aa_context(controller, reverse))["participants"] == [
        "CONTACT_CHEN"
    ]


@pytest.mark.parametrize(
    "text,reason",
    [
        ("我和Lin、同学AA这顿饭300元", "ambiguous"),
        ("和Lin、同学AA 300元", "ambiguous"),
        ("我和Chen、小王AA这顿饭300元", "participants"),
        ("我和小王和ChenAA这顿饭300元", "participants"),
        ("我和Chen、同学AA这顿饭300元，我们四个人", "count_mismatch"),
        ("我和Chen、同学AA这顿饭100元", "uneven"),
        ("我和Chen、同学AA，每人100元", "per_person_phrase"),
        ("这顿饭460我们四个AA", "participants"),
        ("明天我和Chen、同学AA这顿饭300元", "time_not_supported"),
    ],
)
def test_named_aa_rejects_ambiguous_unknown_or_inexact_lists(text, reason):
    controller = _controller()
    result = aa_candidate(text, _aa_context(controller, text))
    assert result["status"] == "unresolved" and result["reason"] == reason
    if reason == "ambiguous":
        assert result["candidates"] == ["CONTACT_LIN_A", "CONTACT_LIN_B"]


def test_named_aa_never_triggers_on_a_payment_sentence():
    controller = _controller()
    text = "把AA款300元转给LIN_A"
    assert aa_candidate(text, _aa_context(controller, text)) == {"status": "none"}
    response = _session(controller).receive(text)
    assert response["status"] == "awaiting_device_selection" and not response["card"]


@pytest.mark.parametrize(
    "text",
    [
        "Chen垫付了300元，我和Chen、同学AA",
        "Chen付了300元，我和Chen、同学AA",
        "我和Chen、同学AA这顿饭300元，Chen买单",
        "Chen请客300元，我和Chen、同学AA",
    ],
)
def test_named_aa_never_collects_from_someone_who_already_paid(text):
    controller = _controller()
    assert aa_candidate(text, _aa_context(controller, text)) == {"status": "none"}
    response = _session(controller).receive(text)
    assert response["status"] == "awaiting_device_selection" and not response["card"]


@pytest.mark.parametrize(
    "text",
    [
        "小王、我和Chen、同学AA这顿饭300元",
        "小王，我和Chen、同学AA这顿饭300元",
        "小王和我和Chen、同学AA这顿饭300元",
        "Chen、我和同学AA这顿饭300元",
    ],
)
def test_named_aa_never_drops_a_name_listed_before_the_speaker(text):
    controller = _controller()
    result = aa_candidate(text, _aa_context(controller, text))
    assert result == {"status": "unresolved", "reason": "participants"}
    assert not _session(controller).receive(text)["card"]


def test_named_aa_history_clarifies_and_together_is_accepted():
    controller = _controller()
    for text in ("上次我和Chen、同学AA了300元", "这顿饭460我们四个AA了"):
        assert aa_candidate(text, _aa_context(controller, text))["reason"] == "historical"
    chat = _session(controller)
    source = chat.receive("这顿饭460我们四个AA了")
    chosen = ["CONTACT_CHEN", "CONTACT_LIN_A", "CONTACT_LANDLORD"]
    with pytest.raises(ValueError):
        chat.prepare_aa(source["response_id"], chosen)
    for text in ("我和Chen、同学一起AA这顿饭300元", "Chen、同学和我一起aa这顿饭300元"):
        result = aa_candidate(text, _aa_context(controller, text))
        assert result["participants"] == ["CONTACT_CHEN", "CONTACT_LIN_A"], result
    assert not controller.state.user_inputs


def test_monthly_chat_card_is_unsigned_red_and_reaches_three_runs():
    chat = _session()
    before = H(chat.controller.state)
    response = chat.receive("每月15号给妈妈转500")
    card = response["card"]
    assert response["status"] == "awaiting_approval" and not response["needs_clarification"]
    assert H(chat.controller.state) == before
    assert not chat.controller.state.user_inputs and not chat.controller.state.schedules
    assert chat.controller.gateway.bank.execute_count == 0
    assert card["kind"] == "schedule_transfer" and card["tier"] == "red"
    assert card["objects"]["contact_ref"] == "CONTACT_CHEN" and card["amount"] == "500/1"
    assert card["spa"]["schedule"] == {"type": "monthly", "day": 15}
    assert card["spa"]["max_runs"] == 3
    assert card["spa"]["start"] == card["execution_window"]["start"] == "2026-11-14T16:00:00Z"
    assert response["payload"]["schedule_echo"]["phrase"] == "每月15号"


def test_chat_and_form_call_the_same_preparer_objects(monkeypatch):
    assert structured_transfer.preview_scheduled_transfer is operations.preview_scheduled_transfer
    assert structured_transfer.preview_structured_transfer is (
        operations.preview_structured_transfer
    )
    assert business_views.preview_aa_collection is operations.preview_aa_collection
    calls = []
    original = operations.preview_scheduled_transfer

    def recorded(*args, **kwargs):
        calls.append((args[1:], kwargs))
        return original(*args, **kwargs)

    monkeypatch.setattr(operations, "preview_scheduled_transfer", recorded)
    chat = _session()
    response = chat.receive("每月15号给妈妈转500", request_id="chat-monthly")
    assert calls == [
        (
            ("A_CURRENT", "CONTACT_CHEN", "500/1", "chat-monthly"),
            {"date_text": "2026-11-15", "monthly_day": 15},
        )
    ]
    form = _controller()
    card = original(
        form,
        "A_CURRENT",
        "CONTACT_CHEN",
        "500",
        "chat-monthly",
        date_text="2026-11-15",
        monthly_day=15,
    )["card"]
    assert _projection(response["card"]) == _projection(card)


def test_chat_aa_and_form_cards_are_equal(monkeypatch):
    calls = []
    original = operations.preview_aa_collection

    def recorded(*args, **kwargs):
        calls.append((args[1:], kwargs))
        return original(*args, **kwargs)

    monkeypatch.setattr(operations, "preview_aa_collection", recorded)
    chat = _session()
    response = chat.receive("我和Chen、同学AA这顿饭300元", request_id="chat-aa")
    assert calls == [
        (("200/1", ["CONTACT_CHEN", "CONTACT_LIN_A"]), {"request_id": "chat-aa"}),
    ]
    assert response["card"]["kind"] == "aa_request" and response["card"]["amount"] == "200/1"
    assert response["card"]["objects"]["participant_refs"] == ["CONTACT_CHEN", "CONTACT_LIN_A"]
    form = original(_controller(), "200", ["CONTACT_CHEN", "CONTACT_LIN_A"])["card"]
    assert _projection(response["card"]) == _projection(form)


def _projection(card):
    identities = {"amount_ref", "schedule_ref", "source_bill_ref", "total_ref"}
    return {
        "kind": card["kind"],
        "objects": {key: value for key, value in card["objects"].items() if key not in identities},
        **{
            key: card[key]
            for key in (
                "amount",
                "tier",
                "tier_reasons",
                "execution_window",
                "total_before",
                "total_after",
                "available",
                "business_date",
            )
        },
        "spa": card["spa"],
    }


def _model(chat, monkeypatch, output):
    monkeypatch.setattr(
        chat.client, "complete", lambda **kwargs: SimpleNamespace(output=dict(output))
    )


def test_model_can_only_downgrade_a_direct_card(monkeypatch):
    chat = _session()
    _model(chat, monkeypatch, {"intent": "transfer", "needs_clarification": True})
    response = chat.receive("每月15号给妈妈转500")
    assert not response["card"] and response["needs_clarification"]
    _model(chat, monkeypatch, {"intent": "balance_query", "needs_clarification": False})
    response = chat.receive("明天给妈妈转50")
    assert response["intent"] == "clarify" and not response["card"]
    before = H(chat.controller.state)
    _model(
        chat,
        monkeypatch,
        {"intent": "transfer", "needs_clarification": False, "amount": "9000/1"},
    )
    with pytest.raises(ValueError):
        chat.receive("我和Chen、同学AA这顿饭300元")
    assert H(chat.controller.state) == before
    assert not chat.controller._pending


def test_red_monthly_lifecycle_controls_and_stop_keeps_it_terminal():
    chat = _session()
    identifier = chat.receive("每月15号给妈妈转500")["response_id"]
    assert chat.confirm(identifier)["status"] == "waiting_mfa"
    assert chat.verify_mfa(identifier)["status"] == "mfa_verified"
    final = chat.confirm(identifier)
    assert final["status"] == "controlled" and final["buttons"] == []
    assert chat.controller.gateway.bank.execute_count == 0
    spa = next(
        item
        for item in chat.controller.state.authorizations.values()
        if type(item).__name__ == "ScheduledPaymentAuthorization"
    )
    assert [due_at(spa, index) for index in range(3)] == [
        "2026-11-14T16:00:00Z",
        "2026-12-14T16:00:00Z",
        "2027-01-14T16:00:00Z",
    ]
    assert chat.confirm(identifier)["status"] == "controlled"
    chat.receive("停")
    assert chat.response(identifier)["status"] == "controlled"
    assert chat.response(identifier)["buttons"] == []


def test_tomorrow_transfer_executes_only_at_the_next_due_time():
    chat = _session()
    identifier = chat.receive("明天给妈妈转50")["response_id"]
    assert chat.confirm(identifier)["status"] == "controlled"
    assert chat.controller.next_due_target() == "2026-10-16T16:00:00Z"
    chat.controller.advance_clock(chat.controller.next_due_target())
    assert chat.controller.gateway.bank.execute_count == 1
    (receipt,) = chat.controller.state.receipts.values()
    debit = receipt.exact_effects[0]
    assert (receipt.kind, receipt.status, debit.amount) == ("transfer", "settled", 50)
    assert debit.counterparty_ref == "DEST_CONTACT_CHEN"


def test_one_jump_past_the_window_expires_the_tomorrow_run():
    chat = _session()
    identifier = chat.receive("明天给妈妈转50")["response_id"]
    chat.confirm(identifier)
    chat.controller.advance_clock("2026-10-18T02:00:00Z")
    assert [run.status for run in chat.controller.state.schedules.values()] == ["expired"]
    assert chat.controller.gateway.bank.execute_count == 0


def test_echo_mismatch_discards_only_this_draft(monkeypatch):
    controller = _controller()
    plan = controller.preview_birthday(event_ref="BIRTHDAY")
    confirmed = controller.confirm_plan(plan["plan_id"], [node["id"] for node in plan["nodes"]])
    assert confirmed["status"] == "settled"
    plans = H(controller.snapshot()["plans"])
    other = operations.preview_aa_collection(controller, "100", ["CONTACT_CHEN"])
    original = operations.preview_scheduled_transfer

    def drifted(controller, source, contact, amount, request_id, **kwargs):
        return original(controller, source, contact, "600/1", request_id, **kwargs)

    monkeypatch.setattr(operations, "preview_scheduled_transfer", drifted)
    signed = len(controller.state.user_inputs)
    response = _session(controller).receive("每月15号给妈妈转500", request_id="drift")
    assert response["status"] == "clarify" and not response["card"] and not response["buttons"]
    assert response["payload"]["echo_mismatch"] is True
    assert "drift" not in controller._pending and other["pending_id"] in controller._pending
    assert H(controller.snapshot()["plans"]) == plans
    assert not [r for r in controller.gateway.audit.records() if "interrupt" in r["event"]]
    assert len(controller.state.user_inputs) == signed


def test_preparer_error_clarifies_and_never_falls_back_to_an_immediate_card(monkeypatch):
    chat = _session()

    def failing(*args, **kwargs):
        raise ValueError("synthetic preparer failure")

    monkeypatch.setattr(operations, "preview_scheduled_transfer", failing)
    monkeypatch.setattr(chat.controller, "preview_transfer", failing)
    response = chat.receive("明天给妈妈转50")
    assert response["status"] == "clarify" and not response["card"]
    assert response["payload"]["prepare_error"] == "ValueError"


def test_im_text_names_schedule_and_participant_facts_from_the_card():
    chat = _session()
    scheduled = render_message(chat.receive("每月15号给妈妈转500"), controller=chat.controller)
    assert scheduled["facts_complete"] and scheduled["buttons"]
    for fact in ("妈妈", "¥500.00", "2026-11-15 00:00", "每月15日·最多3次"):
        assert fact in scheduled["text"], (fact, scheduled["text"])
    aa = chat.receive("我和Chen、同学AA这顿饭300元")
    rendered = render_message(aa, controller=chat.controller)
    assert rendered["facts_complete"] and rendered["buttons"]
    assert "参与人" in rendered["text"] and "同学" in rendered["text"]
    assert "¥100.00" in rendered["text"]
    stripped = {
        **aa,
        "card": {
            **aa["card"],
            "objects": {
                key: value
                for key, value in aa["card"]["objects"].items()
                if key != "participant_refs"
            },
        },
    }
    rendered = render_message(stripped, controller=chat.controller)
    assert not rendered["facts_complete"] and not rendered["buttons"]


def test_aa_chat_confirmation_creates_requests_without_moving_funds():
    chat = _session()
    balances = H(chat.controller.state.accounts)
    identifier = chat.receive("我和Chen、同学AA这顿饭300元")["response_id"]
    result = chat.confirm(identifier)
    assert result["status"] == "settled"
    fields = result["payload"]["analysis_receipt"]["results"]
    assert fields["shares.CONTACT_CHEN"]["value"] == fields["shares.CONTACT_LIN_A"]["value"]
    assert fields["shares.CONTACT_CHEN"]["value"] == "100/1"
    assert len(fields["request_ids"]["value"]) == 2
    assert H(chat.controller.state.accounts) == balances
    assert chat.controller.gateway.bank.execute_count == 0


def test_chat_page_shows_beijing_rows_parse_note_and_controlled_copy():
    from tests.e2e.test_demo_ui import app

    page = app()
    page.button(key="enter").click().run()
    page.text_input(key="domestic_text").set_value("明天给妈妈转50")
    page.button(key="domestic_send").click().run()
    assert not page.exception
    card = next(
        item.value for item in page.markdown if '<section class="approval-card"' in item.value
    )
    assert "2026-10-17 00:00" in card and "单次·授权至 2026-10-18 00:00" in card
    captions = [item.value for item in page.caption]
    assert "解析说明（非授权）" in captions
    assert any("「明天」→ 2026-10-17" in value for value in captions)
    page.button(key="domestic_confirm").click().run()
    assert not page.exception
    session = page.session_state["domestic_session"]
    assert session.response(page.session_state["domestic_response_id"])["status"] == "controlled"
    assert any("定时授权已建立" in item.value for item in page.markdown)
    assert "domestic_confirm" not in {button.key for button in page.button}


def test_p2_chat_completes_a_split_bill_with_the_form_preparer(monkeypatch):
    calls = []
    original = operations.preview_aa_collection

    def recorded(*args, **kwargs):
        calls.append((args[1:], kwargs))
        return original(*args, **kwargs)

    monkeypatch.setattr(operations, "preview_aa_collection", recorded)
    chat = _session()
    source = chat.receive("这顿饭460我们四个AA")
    assert source["status"] == "awaiting_device_selection" and not source["card"]
    shown = chat.response(source["response_id"])
    before = H(chat.controller.state)
    chosen = ["CONTACT_CHEN", "CONTACT_LANDLORD", "CONTACT_LIN_A"]
    completed = chat.prepare_aa(source["response_id"], chosen, request_id="aa-complete")
    assert calls == [(("345/1", chosen), {"request_id": "aa-complete"})]
    assert completed["response_id"] != source["response_id"]
    assert completed["status"] == "awaiting_approval" and completed["buttons"]
    assert completed["card"]["kind"] == "aa_request" and completed["card"]["amount"] == "345/1"
    assert completed["card"]["objects"]["participant_refs"] == chosen
    assert chat.response(source["response_id"]) == shown
    assert H(chat.controller.state) == before and not chat.controller.state.user_inputs
    again = chat.prepare_aa(source["response_id"], chosen)
    assert again["response_id"] == completed["response_id"] and len(calls) == 1
    result = chat.confirm(completed["response_id"])
    assert result["status"] == "settled"
    fields = result["payload"]["analysis_receipt"]["results"]
    assert {fields[f"shares.{ref}"]["value"] for ref in chosen} == {"115/1"}
    assert len(fields["request_ids"]["value"]) == 3
    assert chat.controller.gateway.bank.execute_count == 0


@pytest.mark.parametrize(
    "chosen",
    [
        ["CONTACT_CHEN", "CONTACT_LIN_A"],
        ["CONTACT_CHEN", "CONTACT_LIN_A", "CONTACT_LIN_B", "CONTACT_LANDLORD"],
        ["CONTACT_CHEN", "CONTACT_CHEN", "CONTACT_LIN_A"],
        ["CONTACT_CHEN", "CONTACT_LIN_A", "CONTACT_NOBODY"],
        ["CONTACT_CHEN", "CONTACT_LIN_A", "CONTACT_UNVERIFIED"],
    ],
)
def test_p2_completion_accepts_only_the_remaining_verified_contacts(chosen):
    chat = _session()
    contacts = chat.controller.state.contacts.copy()
    contacts["CONTACT_UNVERIFIED"] = contacts["CONTACT_NEW"].model_copy(
        update={"id": "CONTACT_UNVERIFIED", "verified_at": None, "verification_ref": None}
    )
    chat.controller.gateway.store.update(contacts=contacts)
    source = chat.receive("这顿饭460我们四个AA")
    before = H(chat.controller.state)
    with pytest.raises(ValueError):
        chat.prepare_aa(source["response_id"], chosen)
    assert H(chat.controller.state) == before
    assert not chat.controller._pending


@pytest.mark.parametrize(
    "text",
    [
        "给Chen转200元",
        "明天我和Chen、同学AA这顿饭300元",
        "我们四个AA，每人100元",
        "把AA款300元转给LIN_A",
    ],
)
def test_p2_completion_is_refused_outside_an_unresolved_participant_list(text):
    chat = _session()
    source = chat.receive(text)
    with pytest.raises(ValueError):
        chat.prepare_aa(source["response_id"], ["CONTACT_CHEN", "CONTACT_LIN_A", "CONTACT_LIN_B"])
    assert not chat.controller.state.user_inputs


def test_p2_chat_page_completes_participants_then_shows_the_card():
    from tests.e2e.test_demo_ui import app

    page = app()
    page.button(key="enter").click().run()
    page.text_input(key="domestic_text").set_value("这顿饭460我们四个AA")
    page.button(key="domestic_send").click().run()
    assert not page.exception
    source = page.session_state["domestic_response_id"]
    assert any("补全AA参与人" in item.value for item in page.caption)
    button = page.button(key=f"domestic_aa_complete:{source}")
    assert button.disabled
    page.multiselect(key=f"domestic_aa_participants:{source}").set_value([
        "CONTACT_CHEN",
        "CONTACT_LANDLORD",
        "CONTACT_LIN_A",
    ]).run()
    page.button(key=f"domestic_aa_complete:{source}").click().run()
    assert not page.exception
    session = page.session_state["domestic_session"]
    completed = session.response(page.session_state["domestic_response_id"])
    assert completed["response_id"] != source
    assert completed["card"]["amount"] == "345/1"
    assert "domestic_confirm" in {item.key for item in page.button}
