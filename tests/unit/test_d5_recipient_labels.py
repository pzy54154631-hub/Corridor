"""Recipient display uses one public name without changing authorization facts."""

from types import SimpleNamespace

import pytest

from corridor.crypto.jcs import H
from corridor.i18n import reset_locale, set_locale
from corridor.ui import components
from corridor.ui.controller import DemoController
from corridor.ui.structured_transfer import preview_structured_transfer
from tests.unit.test_dux_cards_dag import DisplayRecorder


@pytest.mark.parametrize("alias", ["妈妈", "老妈", "我妈", "MOM"])
@pytest.mark.parametrize(
    "locale,expected", [("zh-CN", "妈妈（联系人 CHEN）"), ("en", "Mom (Contact CHEN)")]
)
def test_primary_contact_aliases_are_recognized_in_either_display_locale(alias, locale, expected):
    controller = SimpleNamespace(
        state=SimpleNamespace(
            contacts={"CONTACT_CHEN": SimpleNamespace(aliases=["roommate", alias])}
        )
    )
    token = set_locale(locale)
    try:
        assert components.reference_label("CONTACT_CHEN", controller=controller) == expected
    finally:
        reset_locale(token)
    assert controller.state.contacts["CONTACT_CHEN"].aliases == ["roommate", alias]


@pytest.mark.parametrize(
    "locale,expected", [("zh-CN", "妈妈（联系人 CHEN）"), ("en", "Mom (Contact CHEN)")]
)
def test_recipient_primary_label_keeps_aliases_in_evidence(monkeypatch, locale, expected):
    controller = DemoController()
    controller.start_session()
    card = preview_structured_transfer(
        controller, "A_CURRENT", "CONTACT_CHEN", "800", "d5-display"
    )["card"]
    before = (H(controller.state), H(card), controller.gateway.audit.checkpoint())
    display = DisplayRecorder(controller)
    monkeypatch.setattr(components, "st", display)
    monkeypatch.setattr(components, "render_explanation", lambda **kwargs: None)
    token = set_locale(locale)
    try:
        assert components.reference_label("CONTACT_CHEN", controller=controller) == expected
        components.render_card(card, controller=controller)
    finally:
        reset_locale(token)
    visible, evidence = " ".join(display.visible), " ".join(display.evidence)
    assert expected in visible
    for alias in ("roommate", "老妈", "我妈", "MOM"):
        assert alias not in visible
        assert alias in evidence
    assert (H(controller.state), H(card), controller.gateway.audit.checkpoint()) == before
    assert controller.gateway.bank.execute_count == 0


def test_clarification_labels_distinguish_contacts_without_changing_selection(monkeypatch):
    from tests.e2e.test_demo_ui import app

    monkeypatch.setenv("CORRIDOR_LOCALE", "zh-CN")
    page = app()
    page.button(key="enter").click().run()
    page.selectbox(key="demo_preset").select("daily_threshold").run()
    page.button(key="load_demo_preset").click().run()
    page.button(key="demo_example_transfer").click().run()
    controller = page.session_state["demo_scenario"]["controller"]
    before = H(controller.state)
    choices = page.selectbox(key="demo_clarify_contact")
    assert choices.options == ["请选择联系人", "妈妈（联系人 CHEN）", "妈妈（联系人 LIN_A）"]
    choices.select("CONTACT_CHEN").run()
    assert H(controller.state) == before
    assert controller.gateway.bank.execute_count == 1
    assert not page.exception
