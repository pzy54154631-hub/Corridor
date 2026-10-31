"""Billing views derived from gateway analysis receipts, with inspectable sources."""

from datetime import timedelta
from fractions import Fraction
from html import escape

import streamlit as st

from corridor.bench.billing_runner import prepare_analysis
from corridor.schemas.time import parse_time
from corridor.ui import view_mode
from corridor.ui.components import money, render_result
from corridor.ui.controller import _Builder
from corridor.ui.messages import tr


def _field(receipt, name):
    value = receipt["results"][name]
    return {"field": name, "receipt_ref": receipt["receipt_id"], **value}


def billing_projection(result, controller):
    receipt = result.get("analysis_receipt")
    if result.get("status") != "settled" or not receipt:
        return {"totals": {}, "categories": []}
    totals = {
        name: _field(receipt, name)
        for name in ("gross_spend", "refunds", "net_spend")
        if name in receipt["results"]
    }
    categories = [
        {"key": name.split(".", 1)[1], **_field(receipt, name)}
        for name in receipt["results"]
        if name.startswith("category_totals.")
    ]
    return {"totals": totals, "categories": categories}


def anomaly_projection(result, controller):
    receipt = result.get("analysis_receipt")
    if result.get("status") != "settled" or not receipt:
        return []
    cards = []
    for name, value in receipt["results"].items():
        if not name.startswith("anomalies."):
            continue
        reference = name.split(".", 1)[1]
        transaction = controller.state.transactions.get(reference)
        if transaction is None:
            continue
        cards.append({
            **_field(receipt, name),
            "transaction_ref": reference,
            "amount": str(transaction.amount),
            "date": transaction.booked_at,
            "reasons": value["value"],
        })
    return cards


def load_billing(controller, period="2026-09"):
    """Run only existing green query paths after an explicit report request."""
    controller._require_login()
    report = controller.bill_report(period)
    builder = _Builder(controller, controller._id("billing-anomalies"))
    prepared = prepare_analysis(builder, "anomaly_query", period=period)
    anomalies = controller._result(controller.gateway.query(prepared.action, prepared.cert))
    year = period[:4]
    months = sorted({
        (parse_time(row.booked_at) + timedelta(hours=8)).strftime("%Y-%m")
        for row in controller.state.transactions.values()
        if row.owner == controller.device.principal and row.account_ref == "A_CURRENT"
    })
    return {
        "period": period,
        "report": report,
        "anomalies": anomalies,
        "months": {
            month: report if month == period else controller.bill_report(month)
            for month in months
            if month.startswith(year)
        },
    }


def amount_chart(rows, *, trend=False):
    """Responsive SVG geometry uses exact ratios; currency is display-rounded only."""
    width, height = 660, max(112, len(rows) * 56 + 18)
    maximum = max((abs(Fraction(row["value"])) for row in rows), default=Fraction(1)) or 1
    label = tr("Monthly spending" if trend else "Spending categories")
    elements = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        'width="100%" role="img" '
        f'aria-label="{escape(label)}">',
        "<style>text{font:15px sans-serif;fill:#514763}</style>",
    ]
    for index, row in enumerate(rows):
        y = 18 + index * 56
        value = Fraction(row["value"])
        length = max(1, int(abs(value) * 300 / maximum)) if value else 0
        color = "#9f75d6" if value >= 0 else "#23866b"
        elements.extend([
            f'<text x="4" y="{y + 20}">{escape(row["label"])}</text>',
            f'<rect x="128" y="{y}" width="300" height="30" rx="9" fill="#f2edf8"/>',
            f'<rect x="128" y="{y}" width="{length}" height="30" rx="9" fill="{color}"/>',
            f'<text x="644" y="{y + 20}" text-anchor="end">{escape(money(value))}</text>',
        ])
    return "".join([*elements, "</svg>"])


def _source_expander(label, field, controller):
    if not view_mode.is_judge():
        # Customer view: signed receipts and source transactions are judge-view evidence.
        return
    with st.expander(tr("View sources · ") + label):
        st.caption(tr("Technical evidence · signed analysis receipt and referenced transactions"))
        st.json(field)
        sources = [
            controller.state.transactions[ref].model_dump(mode="json")
            for ref in field.get("source_tx_ids", [])
            if ref in controller.state.transactions
        ]
        st.json(sources)


def render_bill_result(result, controller):
    """Render a received report without issuing another query or granting authority."""
    view = billing_projection(result, controller)
    if not view["totals"]:
        render_result(result)
        return
    labels = {
        "gross_spend": tr("Gross spending"),
        "refunds": tr("Refunds"),
        "net_spend": tr("Net spending"),
    }
    for column, (key, value) in zip(st.columns(3), view["totals"].items(), strict=False):
        with column:
            st.metric(labels[key], money(value["value"]))
            _source_expander(labels[key], value, controller)
    st.subheader(tr("Where your money went"))
    rows = [
        {"label": tr(item["key"].replace("_", " ").title()), **item} for item in view["categories"]
    ]
    if rows:
        st.markdown(amount_chart(rows), unsafe_allow_html=True)
        for row in rows:
            _source_expander(row["label"] + " · " + money(row["value"]), row, controller)
    else:
        st.info(tr("No settled spending in this period."))
    if view_mode.is_judge():
        with st.expander(tr("Technical evidence · complete billing receipt")):
            st.json(result)


def render_billing(controller):
    st.header(tr("Bills & spending insights"))
    st.caption(
        tr("Explore settled spending with source records. Green queries need no payment approval.")
    )
    period = st.selectbox(
        tr("Statement period"),
        ["2026-09", "2026-08", "2026-07", "2026"],
        key="billing_period",
    )
    left, right = st.columns(2)
    if left.button(tr("Query current balance"), key="query_balance", use_container_width=True):
        try:
            st.session_state["balance_result"] = controller.query_balance()
        except (ValueError, RuntimeError, OSError):
            st.error(tr("Query incomplete. Enter the simulated bank and try again."))
    if right.button(
        tr("Analyze statement"),
        key="bill_report",
        type="primary",
        use_container_width=True,
    ):
        try:
            bundle = load_billing(controller, period)
            st.session_state["billing_bundle"] = {"controller": controller, "data": bundle}
            st.session_state["bill_result"] = bundle["report"]
        except (ValueError, RuntimeError, OSError):
            st.error(tr("Analysis incomplete. Enter the simulated bank and try again."))
    if st.session_state.get("balance_result"):
        render_result(st.session_state["balance_result"])
    saved = st.session_state.get("billing_bundle")
    if not saved or saved["controller"] is not controller:
        st.info(tr("Choose a period to review categories, monthly trends and suspicious charges."))
        return
    bundle = saved["data"]
    st.caption(tr("Displayed report: ") + bundle["period"])
    render_bill_result(bundle["report"], controller)
    st.subheader(tr("Monthly trend"))
    rows = []
    for month, result in bundle["months"].items():
        receipt = result.get("analysis_receipt")
        if receipt and "net_spend" in receipt["results"]:
            rows.append({"label": month, **_field(receipt, "net_spend")})
    st.caption(
        tr(
            "Only months with transaction records are shown; "
            "missing months are not assumed to be zero."
        )
    )
    st.markdown(amount_chart(rows, trend=True), unsafe_allow_html=True)
    for row in rows:
        _source_expander(row["label"] + " · " + money(row["value"]), row, controller)
    st.subheader(tr("Charges to review"))
    cards = anomaly_projection(bundle["anomalies"], controller)
    if not cards:
        anomaly_receipt = bundle["anomalies"].get("analysis_receipt") or {}
        if bundle["anomalies"].get(
            "status"
        ) == "settled" and "anomaly_tx_ids" in anomaly_receipt.get("results", {}):
            st.success(tr("No suspicious charges were flagged in this period."))
        else:
            st.warning(tr("The anomaly check did not complete; no conclusion is available."))
        if view_mode.is_judge():
            with st.expander(tr("Technical evidence · anomaly check")):
                st.json(bundle["anomalies"])
    for index, item in enumerate(cards, 1):
        with st.container(border=True):
            st.markdown(f"**{tr('Charge to review')} {index} · {money(item['amount'])}**")
            reasons = {
                "duplicate_candidate": tr(
                    "Equal charges from the same merchant occurred close together; "
                    "check for duplicates."
                ),
                "large_expense": tr(
                    "This charge meets the large-expense alert threshold; "
                    "confirm that you recognize it."
                ),
            }
            for reason in item["reasons"]:
                st.warning(
                    reasons.get(
                        reason,
                        tr("The analysis flagged this charge for review."),
                    )
                )
            st.caption(tr("A review flag does not establish fraud."))
            _source_expander(tr("Charge and reason"), item, controller)
