"""Pure-text simulated IM adapter; button clicks are distinct device gestures."""

from decimal import ROUND_HALF_EVEN, Decimal
from fractions import Fraction

from corridor.i18n import get_locale
from corridor.ui.components import (
    KINDS,
    LABELS,
    aa_facts,
    execution_timing,
    object_facts,
    reference_label,
    safe_text,
    schedule_facts,
    tier_reason,
)
from corridor.ui.messages import display_value, tr


def _card_lines(card, controller):
    """Render required approval facts only from the supplied canonical projection."""
    zh = get_locale() == "zh-CN"
    required = {"kind", "objects", "tier", "tier_reasons", "execution_window", "irreversible"}
    if not required <= card.keys() or not isinstance(card["objects"], dict):
        return [], False
    objects = card["objects"]
    kind = card["kind"]
    if kind not in KINDS or card["tier"] not in {"green", "yellow", "red"}:
        return [], False
    if not isinstance(card["tier_reasons"], list) or not isinstance(card["irreversible"], bool):
        return [], False
    if kind == "transfer" and not all(objects.get(key) for key in ("source_ref", "contact_ref")):
        return [], False
    card_operations = {
        "freeze_card",
        "unfreeze_card",
        "report_card_lost",
        "restore_card",
        "lower_card_limit",
        "raise_card_limit",
        "restrict_card",
        "relax_card_restriction",
    }
    if kind in card_operations and not (
        objects.get("card_ref")
        and isinstance(card.get("card_change"), dict)
        and all(card["card_change"].get(field) for field in ("account_ref", "before", "after"))
    ):
        return [], False
    if kind in {
        "subscription_query",
        "subscription_reminders",
        "apply_virtual_card",
    } and not objects.get("account_ref"):
        return [], False
    if kind == "cancel_subscription" and not objects.get("authorization_ref"):
        return [], False
    if kind in {"transfer", "merchant_order", "lock_funds", "unlock_funds", "schedule_transfer"}:
        if not objects.get("source_ref") or card.get("amount") is None:
            return [], False
    if kind == "schedule_transfer" and not card.get("spa"):
        return [], False
    if card["execution_window"] is not None and not (
        isinstance(card["execution_window"], dict)
        and card["execution_window"]
        and all(isinstance(value, str) and value for value in card["execution_window"].values())
    ):
        return [], False
    if kind == "schedule_transfer" and not card["execution_window"]:
        return [], False
    participants = objects.get("participant_refs")
    # A collection request names who is asked to pay; without them nothing is confirmable.
    if kind == "aa_request" and not (
        isinstance(participants, list)
        and participants
        and (controller is None or all(ref in controller.state.contacts for ref in participants))
    ):
        return [], False
    timing = schedule_facts(card) if kind == "schedule_transfer" else {}
    if kind == "schedule_transfer" and not timing:
        return [], False
    lines = [display_value(KINDS[kind])]
    for key in (
        "contact_ref",
        "merchant_ref",
        "source_ref",
        "account_ref",
        "locked_ref",
        "product_ref",
        "card_ref",
        "subscription_ref",
        "authorization_ref",
        "period",
    ):
        value = objects.get(key)
        if value is None:
            continue
        collections = {
            "contact_ref": "contacts",
            "source_ref": "accounts",
            "account_ref": "accounts",
            "locked_ref": "accounts",
            "card_ref": "cards",
            "authorization_ref": "subscriptions",
            "merchant_ref": "merchants",
        }
        if controller is not None and key in collections:
            collection = getattr(controller.state, collections[key])
            if value not in collection:
                return [], False
        shown = reference_label(value, controller=controller)
        title = (
            ("订阅授权" if zh else "Subscription authorization")
            if key == "authorization_ref"
            else display_value(LABELS.get(key, key))
        )
        lines.append(f"{title}: {shown}")
        if key == "authorization_ref" and controller is not None:
            subscription = controller.state.subscriptions[value]
            merchant = reference_label(subscription.merchant_ref, controller=controller)
            source = reference_label(subscription.source_account_ref, controller=controller)
            lines.extend([
                ("商户: " if zh else "Merchant: ") + merchant,
                ("来源账户: " if zh else "Source account: ") + source,
            ])
    for field, title in (
        ("amount", "金额" if zh else "Amount"),
        ("fees", "手续费" if zh else "Fees"),
    ):
        if card.get(field) is not None:
            try:
                value = Fraction(card[field])
                formatted = (Decimal(value.numerator) / Decimal(value.denominator)).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_EVEN
                )
            except (TypeError, ValueError, ZeroDivisionError, ArithmeticError):
                return [], False
            lines.append(f"{title}: ¥{formatted:,.2f}")
    for key, value in aa_facts(card, controller=controller).items():
        lines.append(f"{display_value(LABELS[key])}: {value}")
    # C1 (R-10): the business object the card acts on, the same row the page shows (with
    # its ordinal), so two cards for twin objects never read the same here either.
    for key, value in object_facts(card, controller=controller).items():
        lines.append(f"{display_value(LABELS[key])}: {value}")
    if timing:
        lines.extend(f"{display_value(LABELS[key])}: {value}" for key, value in timing.items())
    else:
        lines.append(("执行时间: " if zh else "Execution time: ") + execution_timing(card))
    lines.append(
        ("级别理由: " if zh else "Approval reason: ") + tier_reason(card, controller=controller)
    )
    lines.append(
        ("规则: " if zh else "Rules: ") + ", ".join(str(rule) for rule in card["tier_reasons"])
    )
    lines.append(
        {
            "green": "绿色" if zh else "Green",
            "yellow": "黄色" if zh else "Yellow",
            "red": "红色" if zh else "Red",
        }[card["tier"]]
    )
    if card["irreversible"]:
        lines.append(
            "不可撤销：已结算付款无法直接撤销；申请退款不代表资金已退回。"
            if zh
            else "Irreversible: a settled payment cannot be undone directly; "
            "a refund request does not mean funds were returned."
        )
    for field in ("card_change", "spa"):
        if isinstance(card.get(field), dict):
            lines.extend(_structured_lines(card[field], controller))
    return lines, True


def _structured_lines(value, controller):
    lines = []
    for key, item in value.items():
        if isinstance(item, dict):
            lines.append(display_value(LABELS.get(key, key)) + ":")
            lines.extend(_structured_lines(item, controller))
        elif key in {
            "source_ref",
            "account_ref",
            "contact_ref",
            "merchant_ref",
            "locked_ref",
            "card_ref",
            "subscription_ref",
            "amount",
            "start",
            "expiry",
            "kind",
            "limit",
            "restrictions",
            "status",
        }:
            shown = (
                reference_label(item, controller=controller)
                if key.endswith("_ref")
                else safe_text(item)
            )
            lines.append(f"{display_value(LABELS.get(key, key))}: {shown}")
    return lines


_OBJECTIVES = {
    "net_return": ("净收益", "Net return"),
    "total_fee": ("总费用", "Total fees"),
    "liquidity": ("流动性", "Liquidity"),
}


def _purchase_lines(purchase, controller, terms=None):
    """B1: a reviewed purchase before its device gesture; facts read from trusted state.

    It is not a canonical card: the gesture signs the preference and the gateway's card
    (with fees, suitability and the best-interest certificate) follows for MFA. C1: the
    reviewed snapshot of every signed preference is listed; one that does not agree with
    the purchase facts makes the review incomplete (no button).
    """
    zh = get_locale() == "zh-CN"
    product, amount = purchase.get("product_ref"), purchase.get("amount")
    source, objective = purchase.get("source_ref"), purchase.get("objective")
    if objective not in _OBJECTIVES or not product or not source:
        return [], False
    if controller is not None and (
        product not in {item.product_id for item in controller.state.catalog.products}
        or source not in controller.state.accounts
    ):
        return [], False
    try:
        value = Fraction(amount)
    except (TypeError, ValueError, ZeroDivisionError):
        return [], False
    if value <= 0 or (value * 100).denominator != 1:
        return [], False
    shown = (Decimal(value.numerator) / Decimal(value.denominator)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_EVEN
    )
    lines = [
        "申购（待设备确认，尚无规范卡）" if zh else "Purchase (awaiting device confirmation)",
        ("产品: " if zh else "Product: ") + reference_label(product, controller=controller),
        ("金额: " if zh else "Amount: ") + f"¥{shown:,.2f}",
        ("扣款账户: " if zh else "Source account: ")
        + reference_label(source, controller=controller),
    ]
    # C1 (R-10): every preference the confirm gesture signs, from the reviewed snapshot.
    if isinstance(terms, dict):
        horizon, access = terms.get("horizon_days"), terms.get("max_access_days")
        types = terms.get("allowed_types")
        if (
            type(horizon) is not int
            or type(access) is not int
            or types != ["wealth"]
            or terms.get("source_account_ref") != source
            or terms.get("selected_product_ref") != product
            or terms.get("amount") != amount
            or terms.get("objective") != objective
        ):
            return [], False
        lines.extend([
            (f"投资期限: {horizon} 天" if zh else f"Investment horizon: {horizon} days"),
            (f"取用等待: 不超过 {access} 天" if zh else f"Access: within {access} days"),
            ("产品类型: 只投理财类产品" if zh else "Product types: wealth products only"),
        ])
    lines.extend([
        ("比较目标: " if zh else "Objective: ") + _OBJECTIVES[objective][0 if zh else 1],
        (
            "确认即签署以上各项并生成规范卡，之后须短信验证（模拟）并最终确认。"
            if zh
            else "Confirm signs every term above and prepares the canonical card; simulated "
            "SMS verification and a final approval follow."
        ),
    ])
    return lines, True


def render_message(response, *, controller=None):
    label = (
        "模拟 IM · 合成数据 · 模拟认证"
        if get_locale() == "zh-CN"
        else "Simulated IM · synthetic data · simulated authentication"
    )
    zh = get_locale() == "zh-CN"
    lines = [label, response["message"]]
    complete = True
    if response["card"]:
        facts, complete = _card_lines(response["card"], controller)
        lines.extend(facts)
        if response.get("recipient_context"):
            lines.append("沿用上一位收款人" if zh else "Using the previous recipient")
    nodes = response["payload"].get("replacement", response["payload"]).get("nodes", [])
    if nodes:
        lines.append("请逐项核对任务卡。" if zh else "Review each task card.")
        for index, node in enumerate(nodes, 1):
            lines.append(f"{index}. {tr(node['label'])}")
            facts, valid = _card_lines(node.get("card", {}), controller)
            lines.extend(facts)
            complete = complete and valid
    candidates = response["payload"].get("candidates") or []
    if response["intent"] == "birthday" and candidates and not nodes:
        # Text only: the IM channel lists trusted calendar events and never selects one.
        lines.append("合成日历中的生日：" if zh else "Birthdays in the synthetic calendar:")
        for item in candidates:
            contact = reference_label(item.get("contact_ref", ""), controller=controller)
            lines.append(
                f"{item.get('event_ref')} · {contact} · "
                + ("生日 " if zh else "birthday ")
                + str(item.get("event_date"))
                + (" · 订购时间 " if zh else " · order time ")
                + str(item.get("order_due"))
            )
    purchase = response["payload"].get("purchase")
    reviewed = False
    if isinstance(purchase, dict) and not response["card"] and not nodes:
        facts, reviewed = _purchase_lines(
            purchase, controller, response["payload"].get("purchase_terms")
        )
        lines.extend(facts)
    if response["buttons"] and not response["card"] and not nodes and not reviewed:
        complete = False
    if not complete:
        lines.append(
            "必需卡片事实未核实，请补全后重新核对。"
            if zh
            else "Required card facts are unverified. Complete them before confirmation."
        )
    lines.append("技术证据可展开查看。" if zh else "Expand technical evidence for details.")
    return {
        **response,
        "buttons": response["buttons"] if complete else [],
        "text": "\n".join(lines),
        "facts_complete": complete,
        "simulated": True,
    }


class IMAdapter:
    def __init__(self, session):
        self.session = session

    def receive(self, text, *, request_id=None):
        return render_message(
            self.session.receive(text, request_id=request_id), controller=self.session.controller
        )

    def click(self, button, response_id, *, selected_node_ids=None):
        current = render_message(
            self.session.response(response_id), controller=self.session.controller
        )
        if not current["facts_complete"]:
            return current
        if button == "confirm":
            response = self.session.confirm(response_id, selected_node_ids=selected_node_ids)
        elif button == "verify_mfa":
            response = self.session.verify_mfa(response_id)
        else:
            raise ValueError("Unknown simulated device gesture")
        return render_message(response, controller=self.session.controller)
