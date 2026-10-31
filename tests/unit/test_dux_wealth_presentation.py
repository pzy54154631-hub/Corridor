"""Wealth presentation keeps machine bindings inspectable and collapsed."""

import re
from copy import deepcopy
from types import SimpleNamespace

import pytest

from corridor.i18n import reset_locale, set_locale
from corridor.ui import wealth_views
from tests.unit.test_dux_cards_dag import DisplayRecorder


@pytest.mark.parametrize("locale", ["zh-CN", "en"])
def test_wealth_dialogue_formats_money_and_collapses_receipt_sources(monkeypatch, locale):
    receipt = "79f1a2cd-743d-5676-b151-af7c66b55b54"
    response = {
        "message": "Please review the recipient.",
        "slots": {"amount": {"value": "10000/1", "source": "user"}},
        "facts": [{"receipt_ref": receipt, "field": "amount", "exact_value": "10000/1"}],
    }
    before = deepcopy(response)
    display = DisplayRecorder()
    monkeypatch.setattr(wealth_views, "st", display)
    monkeypatch.setattr(
        wealth_views,
        "localized_facts",
        lambda *_: [{"text": "CNY 10,000.00", "receipt_ref": receipt, "field": "amount"}],
    )
    token = set_locale(locale)
    try:
        wealth_views.dialogue_response(response, SimpleNamespace())
    finally:
        reset_locale(token)
    visible = " ".join(display.visible)
    assert "10,000.00" in visible
    assert receipt not in visible and "10000/1" not in visible
    assert receipt in " ".join(display.evidence)
    assert "10000/1" in " ".join(display.evidence)
    assert response == before


def test_execution_summary_keeps_complete_receipts_only_in_evidence(monkeypatch):
    receipt = "79f1a2cd-743d-5676-b151-af7c66b55b54"
    execution = {
        "status": "completed",
        "available_balance": "2000/1",
        "items": [
            {
                "product_ref": "P_A",
                "principal": "5000/1",
                "fee_buy": "0/1",
                "status": "settled",
                "receipt_ref": receipt,
            }
        ],
    }
    display = DisplayRecorder()
    monkeypatch.setattr(wealth_views, "st", display)
    wealth_views.execution_summary(execution)
    assert receipt not in " ".join(display.visible)
    assert "5000/1" not in " ".join(display.visible)
    assert receipt in " ".join(display.evidence)
    assert "5000/1" in " ".join(display.evidence)


def test_statement_hover_copy_does_not_expose_exact_fractions():
    from corridor.ui.wealth_statement import build_statement
    from tests.unit.test_ui_wealth_statement import statement_inputs

    statement = build_statement(*statement_inputs())
    titles = re.findall(r'title="([^"]*)"', statement.summary_html)
    assert titles
    assert not any(re.search(r"\d+/\d+", title) for title in titles)
    assert statement.numeric_sources
    assert all("exact" in source for source in statement.numeric_sources.values())
