"""Default domestic bill facts and the externally frozen blind world remain consistent."""

from fractions import Fraction
from hashlib import sha256
from pathlib import Path

import pytest

from corridor.bench.bank_cases import BankCase
from corridor.bench.billing_runner import prepare_analysis
from corridor.bench.blind_zh import ROLES, fixture_state
from corridor.crypto.sign import CATALOG, RECEIPT, verify
from corridor.gateway.analysis import anomaly_results, billing_results
from corridor.gateway.facts import render_narrative
from corridor.gateway.privacy import PIIVault
from corridor.i18n import reset_locale, set_locale
from corridor.schemas.narrative import FactRef, Narrative
from corridor.sim.fixtures import make_state, validate_fixtures, write_fixtures
from corridor.stores.bank import available, daily_total


@pytest.mark.parametrize(
    ("period", "gross", "refunds", "net", "categories"),
    [
        ("2026-09", "660/1", "80/1", "580/1", ("214/1", "40/1", "248/1", "78/1")),
        ("2026-10", "2872/1", "60/1", "2812/1", ("186/1", "58/1", "2539/1", "29/1")),
        ("2026", "3688/1", "140/1", "3548/1", ("400/1", "98/1", "2787/1", "263/1")),
    ],
)
def test_default_month_and_year_reports_have_exact_traceable_spending(
    period, gross, refunds, net, categories
):
    state = make_state()
    results = billing_results(state.transactions.values(), state.merchants, period)
    assert results["gross_spend"].value == gross
    assert results["refunds"].value == refunds
    assert results["net_spend"].value == net
    for category, amount in zip(("dining", "transport", "shopping", "services"), categories):
        item = results[f"category_totals.{category}"]
        assert item.value == amount
        assert item.source_tx_ids
        assert set(item.source_tx_ids) <= state.transactions.keys()
    assert all(row.booked_at <= state.now for row in state.transactions.values())
    # Historical statement rows are already reflected in the opening snapshot.
    # They must not become new execution events or alter the day's transfer total.
    assert available(state, "A_CURRENT") == Fraction(10000)
    assert available(state, "A_LOCKED") == 0
    assert daily_total(state, "C_DEMO") == 0
    assert not state.ledger


def test_default_anomaly_has_a_recomputable_basis_and_a_source():
    state = make_state()
    results = anomaly_results(state.transactions.values(), "2026-10")
    suspicious = results["anomaly_tx_ids"].value
    assert isinstance(suspicious, list) and len(suspicious) == 1
    tx_id = suspicious[0]
    row = state.transactions[tx_id]
    assert row.amount == Fraction(2300)
    assert row.direction == "debit" and row.kind == "merchant_debit"
    reason = results[f"anomalies.{tx_id}"]
    assert reason.value == ["large_expense"]
    assert reason.source_tx_ids == [tx_id]


def test_default_current_bill_executes_green_and_signs_sources():
    case = BankCase(fixture_state(), task_id="DFIX", subcase_id="current-bill")
    prepared = prepare_analysis(case, "bill_analyze", period="2026-10")
    assert prepared.proposal.verdict.tier == "green"
    result = case.dispatch(prepared)
    assert result.status == "settled"
    receipt = result.analysis_receipt
    assert receipt is not None
    assert verify(case.state.roots.gateway_pubkey, RECEIPT, receipt, receipt.sig)
    assert receipt.results["net_spend"].value == "2812/1"
    assert receipt.results["refunds"].source_tx_ids
    assert daily_total(case.state, "C_DEMO") == 0
    assert available(case.state, "A_CURRENT") == Fraction(12000)


@pytest.mark.parametrize(
    ("locale", "basis"),
    [("en", "review threshold"), ("zh-CN", "核对阈值")],
)
def test_anomaly_fact_explains_the_basis_without_changing_signed_reason(locale, basis):
    case = BankCase(fixture_state(), task_id="DFIX", subcase_id="anomaly-display")
    result = case.dispatch(prepare_analysis(case, "anomaly_query", period="2026-10"))
    receipt = result.analysis_receipt
    assert receipt is not None
    tx_id = receipt.results["anomaly_tx_ids"].value[0]
    token = set_locale(locale)
    try:
        rendered = render_narrative(
            Narrative(fact_refs=[FactRef(receipt_ref=receipt.id, field=f"anomalies.{tx_id}")]),
            case.state,
            "C_DEMO",
            PIIVault(),
        )
    finally:
        reset_locale(token)
    assert rendered.facts[0].exact_value == ["large_expense"]
    assert basis in rendered.facts[0].display_value
    assert "2,000" in rendered.facts[0].display_value


def test_aliases_are_trusted_additions_to_existing_contact_objects():
    state = make_state()
    assert {"roommate", "妈妈", "老妈", "我妈", "MOM"} <= set(
        state.contacts["CONTACT_CHEN"].aliases
    )
    assert {"classmate", "同学", "LIN_A"} <= set(state.contacts["CONTACT_LIN_A"].aliases)
    assert {"neighbor", "邻居", "LIN_B"} <= set(state.contacts["CONTACT_LIN_B"].aliases)
    assert {"房东", "LANDLORD"} <= set(state.contacts["CONTACT_LANDLORD"].aliases)
    assert state.contacts["CONTACT_NEW"].is_new


def test_frozen_blind_world_facts_remain_true():
    state = fixture_state()
    assert state.now == "2026-10-16T02:00:00Z"
    assert ROLES == {
        "CUSTOMER": "C_DEMO",
        "CURRENT": "A_CURRENT",
        "LOCKED": "A_LOCKED",
        "MOM": "CONTACT_CHEN",
        "LIN_A": "CONTACT_LIN_A",
        "LIN_B": "CONTACT_LIN_B",
        "NEW_PAYEE": "CONTACT_NEW",
        "CARD": "CARD_DEMO",
        "SUBSCRIPTION": "SUB_AUTH",
        **{value: value for value in ("FLOWERS", "CAKE", "P_A", "P_B", "P_C", "P_D", "P_X", "P_Y")},
    }
    assert state.customers["C_DEMO"].principal == "C_DEMO"
    assert state.risk_assessments[state.customers["C_DEMO"].risk_profile_ref].level == 2
    assert daily_total(state, "C_DEMO") == 0
    for ref, balance in (("A_CURRENT", 12000), ("A_LOCKED", 0)):
        assert state.accounts[ref].status == "active"
        assert state.accounts[ref].ccy == "CNY"
        assert available(state, ref) == balance
    for ref, alias in (
        ("CONTACT_CHEN", "roommate"),
        ("CONTACT_LIN_A", "classmate"),
        ("CONTACT_LIN_B", "neighbor"),
    ):
        assert alias in state.contacts[ref].aliases
        assert not state.contacts[ref].is_new
    assert state.contacts["CONTACT_LIN_A"].name_token == state.contacts["CONTACT_LIN_B"].name_token
    assert state.contacts["CONTACT_NEW"].is_new
    card = state.cards["CARD_DEMO"]
    assert (card.status, card.limit, card.restrictions) == ("active", Fraction(1000), [])
    sub = state.subscriptions["SUB_AUTH"]
    assert (sub.merchant_ref, sub.amount, sub.next_due, sub.status) == (
        "SUB_A",
        Fraction(49),
        "2026-10-18T02:00:00Z",
        "active",
    )
    for ref, amount in (("FLOWERS", 300), ("CAKE", 200)):
        offers = state.merchants[ref].offers
        assert len(offers) == 1 and offers[0].id == "OFFER_" + ref
        assert offers[0].amount == amount
    expected = {
        "P_A": (1, 100, 30, 0, Fraction(3, 100)),
        "P_B": (1, 100, 0, 1, Fraction(1, 50)),
        "P_C": (2, 100, 30, 0, Fraction(1, 25)),
        "P_D": (2, 100, 0, 1, Fraction(3, 100)),
        "P_X": (3, 100, 0, 0, Fraction(9, 100)),
        "P_Y": (1, 20000, 0, 0, Fraction(1, 100)),
    }
    assert {p.product_id for p in state.catalog.products} == expected.keys()
    for product in state.catalog.products:
        assert (
            product.risk_level,
            product.min_amount,
            product.min_hold_days,
            product.redeem_delay_days,
            product.expected_annual_rate,
        ) == expected[product.product_id]
        assert product.purchase_open and product.eligible_customers == ["C_DEMO"]
        assert (
            product.purchase_fee_rate == product.annual_fee_rate == product.redemption_fee_rate == 0
        )
    assert verify(state.roots.bank_pubkey, CATALOG, state.catalog, state.catalog.sig)
    case = BankCase(state, task_id="DFIX", subcase_id="frozen-default-period")
    assert prepare_analysis(case, "bill_analyze").action.args.period == "2026-09"


def test_fixture_regeneration_and_frozen_material_integrity(tmp_path):
    write_fixtures(tmp_path)
    assert validate_fixtures(tmp_path) == make_state()
    materials = Path(__file__).resolve().parents[2] / "data/blind-zh/materials"
    assert sha256((materials / "world.md").read_bytes()).hexdigest() == (
        "241cf070229556b7e77258e782595c9fbc070dc97992ed057e76df407b46f1fb"
    )
    for line in (materials / "MANIFEST.sha256").read_text(encoding="utf-8").splitlines():
        digest, name = line.split(maxsplit=1)
        assert sha256((materials / name).read_bytes()).hexdigest() == digest


@pytest.mark.parametrize(
    ("text", "period", "function_id", "net"),
    [
        ("这个月钱都花哪了", "2026-10", "bill_summary_v1", "2812/1"),
        ("上个月钱都花哪了", "2026-09", "bill_summary_v1", "2379/1"),
        ("出一份今年的年度账单", "2026", "bill_summary_v1", "5521/1"),
        ("有没有我不认识的扣款", "2026-10", "anomaly_v1", None),
    ],
)
def test_actual_chat_uses_current_history_and_signed_analysis(text, period, function_id, net):
    from corridor.agent.domestic_session import DomesticSession
    from corridor.ui.controller import DemoController

    controller = DemoController()
    controller.start_session()
    session = DomesticSession(controller)
    response = session.receive(text)
    assert response["status"] == "settled" and response["tier"] == "green"
    receipt = response["payload"]["analysis_receipt"]
    assert receipt["function_id"] == function_id
    boundaries = {
        "2026-10": ("2026-09-30T16:00:00Z", "2026-10-31T16:00:00Z"),
        "2026-09": ("2026-08-31T16:00:00Z", "2026-09-30T16:00:00Z"),
        "2026": ("2025-12-31T16:00:00Z", "2026-12-31T16:00:00Z"),
    }
    start, end = boundaries[period]
    assert receipt["parameters"]["period"] == {"start_inclusive": start, "end_exclusive": end}
    results = receipt["results"]
    if net is not None:
        assert results["net_spend"]["value"] == net
    else:
        suspicious = results["anomaly_tx_ids"]["value"]
        assert len(suspicious) == 1
        assert results[f"anomalies.{suspicious[0]}"]["value"] == ["large_expense"]
    assert all(item["source_tx_ids"] for item in results.values())
    assert not controller.state.security.consumed_nonces
    assert available(controller.state, "A_CURRENT") == 10000
