"""Streamlit-free preparers shared verbatim by the business forms and the chat channel.

Each function only displays an unsigned canonical card; the later device gesture signs.
"""

from datetime import datetime, timedelta
from fractions import Fraction

from corridor.bench.planning_runner import prepare_spa
from corridor.gateway.handles import available_handles
from corridor.schemas.bank import InputTerm, StateTerm
from corridor.schemas.money import fraction_string, parse_fraction
from corridor.ui.messages import tr


def _choices(controller):
    views = available_handles(controller.state, controller.device.principal, controller.session)
    accounts = [
        item.id for item in views if item.kind == "account" and item.account_kind == "current"
    ]
    contacts = [item.id for item in views if item.kind == "contact" and item.verified]
    return accounts, contacts


def preview_structured_transfer(controller, source, contact, amount, request_id):
    "Retain an unsigned canonical card; the later approval gesture signs its inputs."
    controller._require_login()
    accounts, contacts = _choices(controller)
    if source not in accounts or contact not in contacts:
        raise ValueError(tr("Choose an owned current account and a verified recipient."))
    if not isinstance(amount, str):
        raise ValueError(tr("Enter the amount as exact CNY text."))
    value = parse_fraction(amount.strip())
    if value <= 0 or (value * 100).denominator != 1:
        raise ValueError(tr("Enter a positive CNY amount in whole cents."))
    exact = fraction_string(value)
    controller._claim_request(
        request_id, {"kind": "transfer", "source": source, "contact": contact, "amount": exact}
    )

    def build(builder):
        accounts, contacts = _choices(builder)
        if source not in accounts or contact not in contacts:
            raise ValueError(tr("The selected account or recipient is no longer available."))
        signed = builder.device.confirm_input(
            intent_id=builder.intent(),
            slot="amount",
            value={"amount": exact, "ccy": "CNY"},
            now=builder.state.now,
        )
        selection = builder.device.confirm_input(
            intent_id=builder.intent(),
            slot="contact_ref",
            value=contact,
            now=builder.state.now,
        )
        authorization = builder.operation(
            "transfer", [source, contact], [signed, selection], amount_bound=exact
        )
        return builder.prepare(
            "transfer",
            {"source_ref": source, "contact_ref": contact, "amount_ref": signed.id},
            {
                "source_ref": StateTerm(
                    collection="accounts",
                    object_ref=source,
                    field="id",
                    version=builder.state.accounts[source].version,
                ),
                "contact_ref": StateTerm(
                    collection="contacts",
                    object_ref=contact,
                    field="id",
                    version=builder.state.contacts[contact].version,
                ),
                "amount_ref": InputTerm(input_ref=signed.id, slot="amount"),
            },
            inputs=[signed, selection],
            authorization=authorization,
        )

    return controller._preview_operation(build, request_id=request_id)


def preview_scheduled_transfer(
    controller, source, contact, amount, request_id, *, date_text, monthly_day=None
):
    """Use the existing fixed-source SPA preparer; never infer a date from prose."""
    controller._require_login()
    accounts, contacts = _choices(controller)
    if source != "A_CURRENT" or source not in accounts or contact not in contacts:
        raise ValueError("Scheduled transfers require the supported current account.")
    value = parse_fraction(amount.strip())
    if value <= 0 or (value * 100).denominator != 1:
        raise ValueError("Enter a positive whole-cent amount.")
    try:
        start_time = datetime.strptime(date_text, "%Y-%m-%d") - timedelta(hours=8)
        end_time = start_time + timedelta(days=90 if monthly_day else 1)
    except OverflowError as error:
        raise ValueError(
            "Choose a date whose complete authorization window is representable."
        ) from error
    start = start_time.strftime("%Y-%m-%dT%H:%M:%SZ")
    if start <= controller.state.now or (monthly_day is not None and not 1 <= monthly_day <= 28):
        raise ValueError("Choose a future date and a monthly day from 1 to 28.")
    exact = fraction_string(value)
    end = end_time.strftime("%Y-%m-%dT%H:%M:%SZ")
    controller._claim_request(
        request_id,
        {
            "kind": "schedule_transfer",
            "source": source,
            "contact": contact,
            "amount": exact,
            "start": start,
            "end": end,
            "monthly_day": monthly_day,
        },
    )
    return controller._preview_operation(
        lambda builder: prepare_spa(
            builder,
            amount=exact,
            start=start,
            end=end,
            monthly_day=monthly_day,
            max_runs=3 if monthly_day else 1,
            target={"kind": "transfer", "contact_ref": contact},
        )[0],
        request_id=request_id,
    )


def _state_term(controller, collection, reference):
    item = getattr(controller.state, collection)[reference]
    return StateTerm(collection=collection, object_ref=reference, field="id", version=item.version)


def preview_aa_collection(controller, amount, participants, request_id=None):
    controller._require_login()
    if not isinstance(amount, str):
        raise ValueError("Enter the AA total as exact decimal or rational text")
    try:
        value = Fraction(amount)
    except (ValueError, ZeroDivisionError) as error:
        raise ValueError("Enter a valid AA total") from error
    references = list(participants)
    if value <= 0 or (value * 100).denominator != 1:
        raise ValueError("AA total must be a positive whole-cent amount")
    if not 1 <= len(references) <= 10 or len(set(references)) != len(references):
        raise ValueError("Select one to ten distinct contacts")
    if any(
        ref not in controller.state.contacts
        or controller.state.contacts[ref].owner != controller.device.principal
        for ref in references
    ):
        raise ValueError("Select contacts from the trusted registry")
    exact = fraction_string(value)
    # The form keeps its fresh identity; a chat request binds its own displayed facts.
    request_id = request_id or controller._id("aa-collection")
    controller._claim_request(
        request_id, {"kind": "aa_request", "total": exact, "participants": references}
    )

    def prepare(builder):
        total = builder.device.confirm_input(
            intent_id=builder.intent(),
            slot="total",
            value={"amount": exact, "ccy": "CNY"},
            now=builder.state.now,
        )
        authorization = builder.operation("aa_request", references, [total], amount_bound=exact)
        terms = {
            "source_bill_ref": InputTerm(input_ref=total.id, slot="total"),
            "total_ref": InputTerm(input_ref=total.id, slot="total"),
            **{
                f"participant_refs.{index}": _state_term(controller, "contacts", ref)
                for index, ref in enumerate(references)
            },
        }
        return builder.prepare(
            "aa_request",
            {"source_bill_ref": total.id, "total_ref": total.id, "participant_refs": references},
            terms,
            inputs=[total],
            authorization=authorization,
        )

    return controller._preview_operation(prepare, request_id=request_id)


def query_transactions(controller):
    """The billing page's green transaction-history query, shared verbatim with chat."""
    from corridor.ui.controller import _Builder

    controller._require_login()
    builder = _Builder(controller, controller._id("transaction-query"))
    prepared = builder.prepare(
        "transactions_query",
        {"account_ref": "A_CURRENT"},
        {"account_ref": _state_term(controller, "accounts", "A_CURRENT")},
    )
    return controller._result(controller.gateway.query(prepared.action, prepared.cert))
