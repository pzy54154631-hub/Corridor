"""Product views preserve signed source numbers and observed security failures."""

from fractions import Fraction

import pytest

from corridor.crypto.jcs import H, jcs_bytes
from corridor.i18n import reset_locale, set_locale
from corridor.sim.billing import anomaly_state, report_state
from corridor.ui.controller import DemoController


def test_bill_dashboard_sources_reconcile_to_signed_receipts():
    from corridor.ui.insights import billing_projection, load_billing

    controller = DemoController(initial_state=report_state())
    controller.start_session()
    result = load_billing(controller, "2026-09")
    view = billing_projection(result["report"], controller)
    assert view["totals"]["net_spend"]["value"] == "1700/1"
    assert sum(Fraction(row["value"]) for row in view["categories"]) == 1700
    shopping = next(row for row in view["categories"] if row["key"] == "shopping")
    assert shopping["source_tx_ids"] == ["TX_SHOPPING", "TX_SHOPPING_REFUND"]
    assert "TX_TRANSFER" not in view["totals"]["net_spend"]["source_tx_ids"]
    assert all(item["status"] == "settled" for item in result["months"].values())
    assert controller.gateway.bank.execute_count == 0
    assert not controller.state.user_inputs
    assert not controller.state.security.consumed_nonces


def test_suspicious_cards_use_actual_anomaly_receipt_and_transaction_sources():
    from corridor.ui.insights import anomaly_projection, load_billing

    controller = DemoController(initial_state=anomaly_state())
    controller.start_session()
    result = load_billing(controller, "2026-09")
    cards = anomaly_projection(result["anomalies"], controller)
    assert {item["transaction_ref"] for item in cards} == {
        "TX_DUPLICATE_A",
        "TX_DUPLICATE_B",
        "TX_LARGE",
    }
    large = next(item for item in cards if item["transaction_ref"] == "TX_LARGE")
    assert Fraction(large["amount"]) == controller.state.transactions["TX_LARGE"].amount
    assert large["reasons"] == ["large_expense"]
    assert controller.gateway.bank.execute_count == 0


@pytest.mark.parametrize("locale", ["zh-CN", "en"])
def test_billing_svg_escapes_labels_and_formats_exact_money(locale):
    from corridor.ui.insights import amount_chart

    token = set_locale(locale)
    try:
        svg = amount_chart([{"label": "<unsafe>", "value": "1234/1"}])
    finally:
        reset_locale(token)
    assert "<unsafe>" not in svg and "&lt;unsafe&gt;" in svg
    assert "1,234.00" in svg and "1234/1" not in svg
    assert "viewBox" in svg and 'role="img"' in svg


def test_domestic_privacy_comparison_reports_leaks_separately_from_legal_money(tmp_path):
    from corridor.ui.evaluation import run_attack_comparison

    controller = DemoController()
    controller.start_session()
    before = H(controller.state)
    result = run_attack_comparison("data_extraction", root=tmp_path)
    assert result["attack"]["privacy_observation"]["leaks"] == 0
    assert "PII-OUT" in result["attack"]["rule_ids"]
    assert result["attack"]["funds_moved_cny"] == "200/1"
    assert result["attack"]["prohibited_effects"] == 0
    assert result["baseline"]["attack"]["oracle"]["pii_leaks"] == 1
    assert result["baseline"]["benign"]["completed"]
    assert result["benign"]["completed"]
    assert H(controller.state) == before
    assert b"13900001001" not in jcs_bytes(result)


def test_attack_outcome_does_not_convert_unobserved_or_failed_result_to_success():
    from corridor.ui.attack_views import observed_outcome

    assert observed_outcome({"status": "not_run"}) == "not_run"
    assert observed_outcome({"oracle_indeterminate": True}) == "indeterminate"
    assert observed_outcome({"passed": True, "privacy_observation": {"leaks": 1}}) == "violated"
    assert observed_outcome({"passed": False, "prohibited_effects": 0}) == "failed"


def test_domestic_notification_injection_uses_real_detector_and_clean_twin(tmp_path, monkeypatch):
    from corridor.ui import evaluation

    observations = []
    detector = evaluation.injection_detected

    def observe(texts):
        detected = detector(texts)
        observations.append((texts, detected))
        return detected

    monkeypatch.setattr(evaluation, "injection_detected", observe)
    result = evaluation.run_attack_comparison("prompt_injection", root=tmp_path)
    assert result["attack"]["external_source"] == "subscription_notification"
    assert result["attack"]["subscription_ref"] == "S1"
    assert all("subscription" in values[0].lower() for values, _ in observations)
    assert any(detected for _, detected in observations)
    assert any(not detected for _, detected in observations)
    assert "APR-LOCK" in result["attack"]["rule_ids"]
    assert result["benign"]["completed"]
    assert result["baseline"]["status"] == "run"
    baseline = result["baseline"]
    assert baseline["attack"]["external_text_hash"] == result["attack"]["external_text_hash"]
    assert baseline["benign"]["external_text_hash"] == result["benign"]["external_text_hash"]
    assert "CONTACT_LIN_A" in observations[0][0][0]
    assert baseline["attack"]["attack_success"] is False
    assert baseline["attack"]["bank_dispatches"] == 1
    assert baseline["attack"]["funds_moved_cny"] == "1200/1"
    assert baseline["attack"]["completed"]
    assert baseline["benign"]["completed"]
    assert baseline["attack"]["planner_source"] == "scripted"


def test_safety_lights_and_reasons_follow_real_gateway_state():
    from corridor.ui.safety import create_safety_demo, safety_projection, trigger_signal

    demo = create_safety_demo("injection")
    case = demo["case"]
    normal = safety_projection(case.gateway.safety_snapshot(case.device.principal))
    assert normal["state"] == "Normal" and not normal["reasons"]
    watch = safety_projection(trigger_signal(demo))
    assert watch["state"] == "Watch" and watch["reasons"]
    locked = safety_projection(trigger_signal(demo))
    assert locked["state"] == "Locked" and locked["unlock_after"]
    assert locked["rule_ids"]
