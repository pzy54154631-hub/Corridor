"""facts-i18n-01: the wealth view re-renders guarded facts through the unguarded renderer.

Stated rules:
- gateway/output_guard.py (``_display``): "Nested values are exposed only through
  authenticated leaf projections." ``render_guarded`` therefore publishes the full
  allocation certificate only as a fixed structured-facts sentence carrying the
  "performance benchmark (not guaranteed)" qualifier, and checks every scalar signed
  string for non-ASCII/promissory content before translating it.
- Product view acceptance rule: visible views contain no UUID/hash/canonical fraction
  outside expanded evidence (tests/unit/test_dux_wealth_presentation.py asserts this, but
  replaces ``localized_facts`` with a stub, so the real path is never exercised).

Mechanism: ``WealthDialogue._explain`` (compare_products / why_not) renders the
``allocation`` FactRef through ``render_guarded``.  The UI then calls
``components.localized_facts`` to "refresh the locale", which re-renders the same refs
through the legacy ``gateway.facts.render_narrative``.  That renderer formats the
allocation value with ``translated_literal(str(value))`` -- the Python repr of the entire
BIAC -- so ``wealth_views.dialogue_response`` writes a ~6 KB raw dict containing five
64-hex hashes and canonical return figures such as ``'expected_net': '300/1'`` into the
main view, instead of the guard's sentence.  The output guard's presentation policy is
silently replaced in both locales.
"""

import re

import pytest

from corridor.i18n import reset_locale, set_locale
from corridor.ui import wealth_views
from corridor.ui.components import localized_facts
from tests.unit.test_dux_cards_dag import DisplayRecorder
from tests.unit.test_ui_wealth_controller import prepared_controller


def _compare_turn(locale):
    token = set_locale(locale)
    try:
        controller = prepared_controller()
        response = controller.receive_goal("Compare P_A and P_B", request_id="rt5-fi-01")
        return controller, response
    finally:
        reset_locale(token)


@pytest.mark.parametrize("locale", ["zh-CN", "en"])
def test_facts_i18n_01_locale_refresh_equals_the_guarded_rendering(locale):
    controller, response = _compare_turn(locale)
    assert response["tool"] == "compare_products"
    guarded = response["facts"]
    assert [fact["field"] for fact in guarded] == ["allocation"]
    token = set_locale(locale)
    try:
        refreshed = localized_facts(guarded, controller)
    finally:
        reset_locale(token)
    # Same receipt, same field, same locale: a language refresh must not change what
    # the output guard decided to publish.
    assert [fact["text"] for fact in refreshed] == [fact["text"] for fact in guarded]
    assert [fact["display_value"] for fact in refreshed] == [
        fact["display_value"] for fact in guarded
    ]


@pytest.mark.parametrize("locale", ["zh-CN", "en"])
def test_facts_i18n_01_wealth_view_shows_no_raw_certificate(monkeypatch, locale):
    controller, response = _compare_turn(locale)
    guarded_text = response["facts"][0]["text"]
    display = DisplayRecorder(controller)
    monkeypatch.setattr(wealth_views, "st", display)
    token = set_locale(locale)
    try:
        wealth_views.dialogue_response(response, controller)
    finally:
        reset_locale(token)
    visible = " ".join(display.visible)
    assert guarded_text in visible
    assert not re.search(r"[0-9a-f]{64}", visible), "binding hashes in the main view"
    assert "'expected_net'" not in visible, "raw return figures without the qualifier"
    assert not re.search(r"'-?\d+/\d+'", visible), "canonical fractions in the main view"
