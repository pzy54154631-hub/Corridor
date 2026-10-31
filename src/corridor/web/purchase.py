"""Read-only binding for the native chat purchase preference review.

This is not a gateway card. The existing IM confirm is the sole signing operation;
after it, the task follows the ordinary pending-card state machine.
"""

from copy import deepcopy

from corridor.web import compat

TERM_FIELDS = frozenset({
    "version",
    "source_account_ref",
    "amount",
    "horizon_days",
    "max_access_days",
    "allowed_types",
    "objective",
    "selection_mode",
    "selected_product_ref",
    "reason_code",
})


def snapshot(ws, rid):
    response = ws.session.response(rid)
    payload = response["payload"]
    purchase = payload.get("purchase")
    terms = payload.get("purchase_terms")
    if not isinstance(purchase, dict) or not isinstance(terms, dict):
        return None
    state = ws.controller.state
    product = next(
        (p for p in state.catalog.products if p.product_id == purchase.get("product_ref")), None
    )
    account = state.accounts.get(purchase.get("source_ref"))
    if product is None or account is None:
        return None
    return deepcopy({
        "browser": ws.browser_id,
        "tab": ws.tab_id,
        "generation": ws.generation,
        "owner": ws.controller.device.principal,
        "rid": rid,
        "rkey": ws.rkey_of.get(rid),
        "mode": compat.envelope_mode(ws.session, rid),
        "purchase": purchase,
        "terms": terms,
        "product": product.model_dump(mode="json"),
        "account": account.model_dump(mode="json"),
    })


def current_terms(ws, task):
    review = task.review or {}
    purchase = review.get("purchase") or {}
    if not all(purchase.get(key) for key in ("product_ref", "amount", "objective")):
        return None
    return ws.session.purchase_review_terms(
        purchase["product_ref"], purchase["amount"], purchase["objective"]
    )


# The review shows the amount in yuan; the signed amount input names this currency.
CURRENCY = "CNY"


def valid(ws, task):
    review = task.review
    if review is None or snapshot(ws, task.rid) != review:
        return False
    response = ws.session.response(task.rid)
    payload = response["payload"]
    terms = review["terms"]
    return (
        review["mode"] == "purchase"
        and response["status"] == payload.get("status") == "awaiting_input"
        and payload.get("canonical") is False
        and not response.get("card")
        and any(button.get("id") == "confirm" for button in response["buttons"])
        and set(terms) == TERM_FIELDS
        and terms == current_terms(ws, task)
        # The account the signed preference names is the one the review shows, the
        # customer's own, in the currency shown (R3-E4).
        and terms.get("source_account_ref") == review["purchase"].get("source_ref")
        and review["account"]["id"] == review["purchase"].get("source_ref")
        and review["account"]["owner"] == review["owner"]
        and review["account"].get("ccy") == CURRENCY
        and bool(compat.render_message(response, controller=ws.controller)["facts_complete"])
    )


def latest(ws, task):
    """Returning to an older reply cannot resurrect unsigned purchase preferences."""
    return (
        next((turn.rid for turn in reversed(ws.transcript) if turn.role == "user"), None)
        == task.rid
    )
