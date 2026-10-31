"""UI-POLISH P2: readable synthetic names are a display projection only."""

import ast
import re
from copy import deepcopy
from pathlib import Path

import pytest

from corridor.crypto.jcs import H
from corridor.i18n import reset_locale, set_locale
from corridor.ui import display_names
from corridor.ui.audience import reset_audience, set_audience
from corridor.ui.controller import DemoController, demo_state
from corridor.ui.messages import UI_MESSAGES
from corridor.ui.wealth_controller import WealthController

ROOT = Path(__file__).resolve().parents[2]
RISK = re.compile(r"\b[RC][1-5]\b|[RC][1-5](?![0-9])")


def _catalogs():
    return {
        "bank": demo_state(showcase=True),
        "wealth": WealthController().state,
    }


def _locale(locale):
    return set_locale(locale)


def test_every_catalog_product_has_a_distinct_signature_name_without_risk_level():
    keys = {}
    for origin, state in _catalogs().items():
        for product in state.catalog.products:
            key = display_names.product_key(product)
            assert key in UI_MESSAGES, (origin, product.product_id, key)
            keys[(origin, product.product_id)] = key
    assert len(keys) == 11
    assert len(set(keys.values())) == 11
    assert keys[("bank", "P_C")] != keys[("wealth", "P_C")]
    for key in keys.values():
        for locale in ("zh-CN", "en"):
            assert not RISK.search(UI_MESSAGES[key][locale]), key


def test_bank_and_wealth_p_c_get_different_names():
    states = _catalogs()
    token = _locale("zh-CN")
    try:
        bank = display_names.label("P_C", states["bank"], trace=False)
        wealth = display_names.label("P_C", states["wealth"], trace=False)
    finally:
        reset_locale(token)
    assert bank and wealth and bank != wealth


def test_names_are_marked_synthetic_and_tails_are_not_vault_values():
    names = [
        key
        for key in UI_MESSAGES
        if key.startswith((
            "polish.product.",
            "polish.account.",
            "polish.merchant.",
            "polish.card.",
        ))
    ]
    assert names
    for key in names:
        assert "合成" in UI_MESSAGES[key]["zh-CN"], key
        assert "synthetic" in UI_MESSAGES[key]["en"], key
    controller = DemoController()
    vault_values = list(controller.gateway.vault._values.values())
    tails = [UI_MESSAGES[key]["zh-CN"] for key in UI_MESSAGES if key.startswith("polish.tail.")]
    assert len(tails) == 6
    for tail in tails:
        assert re.fullmatch(r"\d{4}", tail)
        assert not any(tail in str(value) for value in vault_values)


def test_unknown_reference_falls_back_and_judge_view_keeps_identifier():
    state = demo_state(showcase=True)
    assert display_names.label("P_UNKNOWN", state) is None
    assert display_names.label("MERCHANT_UNKNOWN", state) is None
    assert display_names.contact_label("CONTACT_UNKNOWN", state) is None
    token = _locale("zh-CN")
    try:
        assert display_names.label("P_A", state).endswith(" · P_A")
        audience = set_audience("customer")
        try:
            assert display_names.label("P_A", state) == "稳健30天持有（合成）"
            assert display_names.contact_label("CONTACT_CHEN", state) == "妈妈 · 尾号3721（合成）"
            assert display_names.time_text("2026-10-16T02:30:00Z") == (
                "2026-10-16 10:30:00（北京时间）"
            )
        finally:
            reset_audience(audience)
        assert display_names.time_text("2026-10-16T02:30:00Z") == "2026-10-16T02:30:00Z"
        assert display_names.outcome_label("Accept") == "通过（Accept）"
        assert display_names.event_label("kernel_verdict") == "内核裁决（kernel_verdict）"
        assert display_names.event_label("not_an_event") == "not_an_event"
    finally:
        reset_locale(token)


def test_rendering_names_never_changes_state_or_cards():
    from corridor.ui import components

    controller = DemoController(initial_state=demo_state(showcase=True))
    controller.start_session()
    controller.compare_investments("net_return")
    card = controller.preview_investment(None, request_id="NAMES")["card"]
    before_state, before_card = H(controller.state), deepcopy(card)
    for reference in ("P_A", "A_CURRENT", "A_LOCKED", "FLOWERS", "CARD_UI_DEMO", "CONTACT_CHEN"):
        components.reference_label(reference, controller=controller)
        display_names.label(reference, controller.state)
    assert H(controller.state) == before_state
    assert card == before_card


def _imports(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(item.name for item in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
    return names


def test_display_names_module_boundaries():
    imports = _imports(ROOT / "src/corridor/ui/display_names.py")
    assert not any(
        name == banned or name.startswith(banned + ".")
        for name in imports
        for banned in ("streamlit", "corridor.gateway", "corridor.kernel")
    )
    for folder in ("agent", "channels"):
        for path in (ROOT / "src/corridor" / folder).rglob("*.py"):
            assert "display_names" not in path.read_text(encoding="utf-8"), path


def test_catalog_merges_without_conflict_and_literal_category_keys_are_unique():
    from corridor.schemas.base import loads_exact

    fragments = [
        ROOT / "src/corridor/ui/messages.json",
        *sorted((ROOT / "src/corridor/ui").glob("messages-*.json")),
    ]
    merged = {}
    for path in fragments:
        for key, value in loads_exact(path.read_bytes()).items():
            # The import-time merge rejects a key repeated with a different value.
            assert key not in merged or merged[key] == value, key
            merged[key] = value
    for key in ("overseas", "online", "cash", "entertainment"):
        count = sum(key in loads_exact(path.read_bytes()) for path in fragments)
        assert count == 1, key
        assert UI_MESSAGES[key]["zh-CN"]


@pytest.mark.parametrize("locale", ["zh-CN", "en"])
def test_position_label_names_the_product(locale):
    state = demo_state(showcase=True)
    token = _locale(locale)
    audience = set_audience("customer")
    try:
        label = display_names.position_label(state, "POSITION_UI_DEMO", 1)
    finally:
        reset_audience(audience)
        reset_locale(token)
    assert ("合成" if locale == "zh-CN" else "synthetic") in label
