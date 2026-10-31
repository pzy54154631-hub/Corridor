"""B2: card view models (design §3.4, §3.5.2, H11, INV-1.4).

A CardVM is built only from the controller's authoritative card; every fact row names
the card field it was read from, and its label and value follow ``render_card`` and
``_fact_rows_html``. Notes are never facts, and judge evidence never reaches a customer.
"""

import re

import pytest

import corridor.web as web_package
from corridor.web import compat
from corridor.web.catalog import bound, wtr
from corridor.web.compat import (
    LABELS,
    display_value,
    execution_timing,
    money,
    reference_label,
    tr,
)
from corridor.web.vm import cards as card_vms
from tests.unit.test_ui_scam_friction import FRAUD, NEW, SAFE
from tests.web.test_viewmodels import RED, YELLOW, Web, without_key

pytestmark = pytest.mark.usefixtures("st_sentinel")
BIRTHDAY = "帮我准备老婆的生日"
SCHEDULED = "10月20日给妈妈转300元"
TRANSFER_PATHS = [
    "objects.contact_ref",
    "objects.source_ref",
    "fees",
    "execution_window",
    "available",
    "total_after",
]


@pytest.fixture
def plans(monkeypatch):
    monkeypatch.setattr(
        web_package, "FEATURES", web_package.FEATURES | {"plan_select", "tab:plans"}
    )


def authoritative(web, rkey):
    task = web.ws.tasks["T" + rkey]
    return compat.authoritative_cards(web.controller, task.identity)


def test_transfer_fact_rows_carry_their_field_paths_labels_and_values():
    web = Web(view="judge").login()
    rkey = web.send(YELLOW)
    card = web.response(rkey)["cards"][0]
    source = authoritative(web, rkey)[0]
    assert [fact["key"] for fact in card["facts"]] == TRANSFER_PATHS
    with bound(web.ws):
        expected = [
            display_value(LABELS[name])
            for name in (
                "contact_ref",
                "source_ref",
                "fees",
                "execution_window",
                "available",
                "total_after",
            )
        ]
        values = [
            reference_label(source["objects"]["contact_ref"], controller=web.controller),
            reference_label(source["objects"]["source_ref"], controller=web.controller),
            tr("As shown on the bank receipt"),
            execution_timing(source),
            money(source["available"]),
            money(source["total_after"]),
        ]
    assert [fact["label"] for fact in card["facts"]] == expected
    assert [fact["value"] for fact in card["facts"]] == values
    assert card["amount_text"] == "¥500.00" and card["purpose"] == "转账"
    assert card["tier"] == {"code": "yellow", "label": "黄色 · 需用户确认", "glyph": "diamond"}
    assert all(re.fullmatch(r"[a-z_][a-z0-9_.]*", fact["key"]) for fact in card["facts"])


def test_notes_and_the_two_questions_are_never_card_facts():
    web = Web().login()
    rkey = web.send(FRAUD, 1)
    response = web.response(rkey)
    card = response["cards"][0]
    assert [note["kind"] for note in card["notes"]] == ["fraud", "irreversible", "red"]
    fraud = card["notes"][0]
    assert fraud["title"] and fraud["footer"] and len(fraud["lines"]) >= 2
    shown = " ".join(fact["label"] + fact["value"] for fact in card["facts"])
    noted = [line for note in card["notes"] for line in note["lines"]]
    gate = response["gate"]
    for text in [*noted, gate["title"], gate["footer"], *gate["facts"]]:
        assert text not in shown
    assert card["not_signed_label"] == wtr("web.vm.not_signed")


def test_a_new_payee_card_carries_the_fraud_reminder_outside_the_frame():
    web = Web().login()
    rkey = web.send(NEW)
    card = web.response(rkey)["cards"][0]
    assert card["tier"]["code"] == "red"
    assert [note["kind"] for note in card["notes"]][0] == "fraud"
    assert all(fact["key"] != "fraud" for fact in card["facts"])


def test_customer_cards_name_synthetic_contacts_and_carry_no_evidence():
    web = Web().login()
    rkey = web.send(YELLOW)
    card = web.response(rkey)["cards"][0]
    assert card["facts"][0]["value"] == "妈妈 · 尾号3721（合成）"
    assert "technical" not in card and "structured" not in card
    assert card["why"] == {"lines": []}
    assert card["title"] == "请核对办理信息"


def test_judge_cards_carry_the_canonical_card_and_its_reasons():
    web = Web(view="judge").login()
    rkey = web.send(RED)
    card = web.response(rkey)["cards"][0]
    source = authoritative(web, rkey)[0]
    assert card["technical"]["card"] == source
    assert card["technical"]["amount_raw"] == "5000/1"
    assert "妈妈" in card["technical"]["contact_aliases"]
    assert card["why"]["lines"][0].startswith(tr("Approval tier: "))
    assert card["why"]["lines"][1] == tr("tier_reason.daily_limit")


def test_both_panes_render_one_card_list_through_one_function(monkeypatch):
    web = Web().login()
    rkey = web.send(FRAUD)
    web.answer(SAFE)
    web.gesture()
    page = web.state()
    response = page["chat"]["responses"][rkey]
    task = page["device"]["tasks"][0]
    assert task["step"] == "mfa_request"
    assert without_key(response["cards"]) == without_key(task["cards"])
    calls = []
    original = compat.authoritative_cards

    def spy(controller, identity):
        calls.append(identity)
        return original(controller, identity)

    monkeypatch.setattr(compat, "authoritative_cards", spy)
    web.state()
    assert web.ws.tasks[task["key"]].identity in calls


def test_card_view_models_are_built_from_a_card_dict_alone():
    web = Web(view="judge").login()
    rkey = web.send(YELLOW)
    source = authoritative(web, rkey)[0]
    with bound(web.ws):
        built = card_vms.card_vm(source, controller=web.controller, key="X-0")
    shown = web.response(rkey)["cards"][0]
    assert {**built, "key": shown["key"]} == shown


def test_a_scheduled_transfer_shows_its_first_run_and_period():
    web = Web().login()
    rkey = web.send(SCHEDULED)
    card = web.response(rkey)["cards"][0]
    keys = [fact["key"] for fact in card["facts"]]
    assert keys[:5] == [
        "objects.contact_ref",
        "objects.source_ref",
        "fees",
        "first_run",
        "schedule_period",
    ]
    with bound(web.ws):
        assert card["facts"][3]["label"] == display_value(LABELS["first_run"])
        assert card["facts"][4]["label"] == display_value(LABELS["schedule_period"])
    assert [note["kind"] for note in card["notes"]][-1] == "schedule_scope"


def test_plan_nodes_are_unsigned_device_review_facts_and_the_device_shows_the_selection(plans):
    web = Web().login()
    rkey = web.send(BIRTHDAY)
    response = web.response(rkey)
    assert len(response["cards"]) == 4
    first = response["cards"][0]
    with bound(web.ws):
        assert first["title"] == tr("Facts for device review · not yet authorized")
        assert first["position"] == tr("Payment {position} of {total}", position=1, total=4)
    select = response["plan_select"]
    assert [node["key"] for node in select["nodes"]] == ["n0", "n1", "n2", "n3"]
    assert select["mode"] == "none" and select["tkey"] == "T" + rkey
    task = web.task(rkey)
    assert task["step"] == "select_nodes" and task["cards"] == [] and task["gesture"] is None
    web.post("select_nodes", task["key"], task["nodes_version"], ["n0"])
    task = web.task(rkey)
    assert task["step"] == "confirm_subset" and task["gesture"]["label"] == "生成新卡"
    assert [card["key"] for card in task["cards"]] == [f"T{rkey}-0"]
    assert without_key(task["cards"]) == without_key(web.response(rkey)["cards"][:1])
    assert task["selection"]["count"] == 1 and task["selection"]["total"] == 4
    web.post("select_nodes", task["key"], task["nodes_version"], ["n0", "n2"])
    response = web.response(rkey)
    assert response["plan_select"]["mode"] == "invalid"
    assert response["plan_select"]["error_text"] == wtr("web.vm.plan.error")
    task = web.task(rkey)
    assert task["step"] == "select_nodes" and task["gesture"] is None
    assert [card["key"] for card in task["cards"]] == [f"T{rkey}-0", f"T{rkey}-2"]
