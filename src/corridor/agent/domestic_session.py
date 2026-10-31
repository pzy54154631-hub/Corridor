"""Shared conversation channel with no implicit device authorization."""

import re
from copy import deepcopy
from decimal import ROUND_HALF_EVEN, Decimal
from fractions import Fraction

from corridor.agent import domestic_suggest
from corridor.agent.birthday_event import relation_in_text
from corridor.agent.domestic_intent import (
    SCENARIOS,
    IntentCandidate,
    compensation_request,
    history_listing,
    natural_stop,
    negation_hold,
    normalized_transfer,
    recognize,
    scam_phrase_hold,
)
from corridor.agent.domestic_mood import mood, names_a_time
from corridor.agent.domestic_negation import PLAN_CANCEL, negation_words
from corridor.agent.domestic_schedule import (
    aa_candidate,
    cancel_negated,
    schedule_candidate,
    standing_cancel,
    standing_transfer,
)
from corridor.agent.domestic_slots import (
    extract_slots,
    has_date_request,
    money_span,
    trusted_slot_context,
)
from corridor.agent.domestic_subscription import cancel_candidates
from corridor.agent.domestic_wealth import (
    amount_candidate,
    product_candidates,
    redemption_quantity,
    wealth_action,
    without_products,
)
from corridor.i18n import get_locale
from corridor.schemas.money import fraction_string
from corridor.sim.keys import stable_id
from corridor.stores.bank import add_seconds, business_date

_MESSAGES = {
    "preview": (
        "请核对规范卡片，并在模拟设备上明确确认。",
        "Review the canonical card and confirm on the simulated device.",
    ),
    # "This message executed nothing": earlier steps of a running plan may have executed.
    "clarify": (
        "请补充明确对象、金额或日期；本条消息没有执行任何操作。",
        "Confirm the exact object, amount or date; this message executed no operation.",
    ),
    "when": (
        "您想什么时候转？请说明确的日期，例如“明天”“下周五”“10月20号”或“每月5号”；"
        "本条消息没有执行任何操作。",
        "When should it be sent? Name an exact date such as tomorrow, next Friday, 20 October "
        "or the 5th of every month; this message executed no operation.",
    ),
    "number_not_date": (
        "句中的“N号”看起来是房号、楼号或门牌，不当作日期。您想什么时候转？"
        "请说明确的日期；本条消息没有执行任何操作。",
        "The day number reads as a room, building or door number, not a date. When should it "
        "be sent? Name an exact date; this message executed no operation.",
    ),
    # S5: a money-moving request held by the negation gate.
    "negated": (
        "这句话里有否定或取消的说法（如“不”“别”“取消”“停止”），没有生成新的卡片；"
        "本条消息没有执行任何操作。如需办理，请去掉否定说法后重新说明；"
        "如需中断进行中的计划或撤回待确认的卡片，请说“停止”。",
        "The request contains a negation or cancel word (such as not, don't, cancel or stop), "
        "so no new card was created; this message executed no operation. To proceed, restate "
        "it without the negation; to interrupt a running plan or withdraw a pending card, say "
        "stop.",
    ),
    # S6 R7: a money-moving request held by the phrase gate (an impersonated authority and
    # a freeze / safe-account / transfer-now demand). {authority} and {pressure} are fixed
    # lexicon labels, never the user's own words.
    "scam_phrase": (
        "您这句话提到了「{authority}」，还提到了「{pressure}」。银行、公安和反诈中心不会让您"
        "向“安全账户”转账，也不会因为您不转账就冻结账户。这笔付款没有生成，也没有执行任何"
        "操作。如需核实，请先挂断对方电话，拨打 96110 或银行卡背面的官方客服电话。",
        "You mentioned {authority} and also {pressure}. Banks, the police and anti-fraud "
        "centres never ask you to move money to a 'safe account', and never freeze your "
        "account because you did not transfer. This payment was not created and nothing was "
        "executed. To check, hang up first, then call 96110 or the official number on the "
        "back of your card.",
    ),
    "stop_none": (
        "当前没有进行中的计划或待确认的卡片；本条消息没有执行任何操作。",
        "There is no running plan or pending card; this message executed no operation.",
    ),
    # C5: a stop phrase inside a sentence leaves established standing authorizations.
    "stop_kept_standing": (
        "已建立的定时转账授权没有取消；如需取消，请说明要取消的那一项"
        "（例如“取消每月5号给房东的转账”），我会列出来供您选择。",
        " Established scheduled transfer authorizations were not cancelled; to cancel one, "
        "name it (for example, cancel the monthly transfer to the landlord on the 5th) and "
        "it is listed for you to choose.",
    ),
    "compensation_card": (
        "已按任务面板“补偿”的同一流程生成补偿卡：把该计划仍锁定的资金退回活期。"
        "请核对卡片并在模拟设备上确认；聊天不会代签。",
        "The task panel's own compensation flow prepared a card returning the plan's locked "
        "funds to the current account. Review it and confirm on the simulated device; chat "
        "cannot sign it.",
    ),
    # These two may follow an interrupt receipt, so they make no "nothing happened" claim.
    "compensation_none": (
        "该计划没有可自动退回的锁定资金；已结算的付款不能直接撤销，也没有生成补偿卡。",
        "The plan has no locked funds to return automatically; settled payments cannot be "
        "undone directly and no compensation card was created.",
    ),
    "compensation_error": (
        "补偿卡未能生成，请在任务面板核对计划状态后重试。",
        "The compensation card could not be prepared. Review the plan in the task panel and retry.",
    ),
    "compensation_no_plan": (
        "当前没有已登记的计划，没有可退回的锁定资金；本条消息没有执行任何操作。",
        "There is no registered plan and no locked funds to return; this message executed "
        "no operation.",
    ),
    "compensation_active": (
        "计划仍在进行，补偿前需要先中断。请先说“停止”中断后续步骤，再申请退回；"
        "本条消息没有执行任何操作。",
        "The plan is still running and must be interrupted before compensation. Say stop "
        "first, then ask for the refund; this message executed no operation.",
    ),
    "wealth": (
        "已识别理财需求。请进入理财/设备页完成风险测评和目标确认；聊天不会代签申购。",
        "Open Wealth and Device to complete the risk profile and signed goals; "
        "chat cannot sign a purchase.",
    ),
    "refused": (
        "抱歉，这项请求不在可办业务范围。可以办理转账、账单、理财、卡片、订阅和生日安排。",
        "This request is outside supported banking services. Transfers, billing, wealth, "
        "cards, subscriptions and birthday planning are available.",
    ),
    "password": (
        "密码修改须使用银行独立强验证渠道；本模拟系统不接收密码或验证码，也不执行修改。",
        "Use the bank's independent strong-verification channel for password changes. "
        "This simulator neither collects credentials nor changes passwords.",
    ),
    "device": (
        "请在对应业务页选择明确对象和完整参数，再由设备确认。",
        "Select the exact object and complete parameters on the business page "
        "before device confirmation.",
    ),
    "result": (
        "当前状态来自确定性网关流程；仅核验成功的银行回执表示交易成功。",
        "Status comes from the deterministic gateway workflow. "
        "Only a verified bank receipt confirms successful execution.",
    ),
    "login": (
        "请先进入模拟银行，确认设备登录。",
        "Enter the simulated bank and confirm device login first.",
    ),
    "withdrawn": (
        "该提案已撤回，请重新发起并核对新卡片。",
        "This proposal was withdrawn. Create and review a new card.",
    ),
    "birthday_select": (
        "没有唯一匹配的生日，请从合成日历中选择要安排的生日；本条消息没有执行任何操作。",
        "No single birthday matches. Choose one in the synthetic calendar; "
        "this message executed no operation.",
    ),
    "card_mismatch": (
        "生成的卡片与对话解析的参数不一致，已丢弃该草稿；请在业务页核对后重试。"
        "本条消息没有执行任何操作。",
        "The prepared card did not match the parsed request and was discarded. Review it on "
        "the business page; this message executed no operation.",
    ),
    "birthday_window": (
        "该生日不在锁款窗口内（订购日所在月份、订购时间之前），暂不生成计划；"
        "本条消息没有执行任何操作。",
        "This birthday is outside its budget-lock window (the order day's month, before the "
        "order time); no plan or operation was created.",
    ),
    # B1: cancelling a subscription in chat names one of the customer's own authorizations.
    "subscription_choose": (
        "您有以下可取消的代扣授权：{items}。请说出要取消的那一项（例如每月金额或商户名），"
        "或在订阅页选择；本条消息没有执行任何操作。",
        "These debit authorizations can be cancelled: {items}. Say which one to cancel (for "
        "example its monthly amount or merchant), or choose it on the subscriptions page; this "
        "message executed no operation.",
    ),
    "subscription_no_match": (
        "没有找到与您说的金额或商户相符的代扣授权。可取消的有：{items}。"
        "请说出要取消的那一项；本条消息没有执行任何操作。",
        "No debit authorization matches the amount or merchant you named. These can be "
        "cancelled: {items}. Say which one to cancel; this message executed no operation.",
    ),
    "subscription_none": (
        "当前没有可取消的代扣授权；本条消息没有执行任何操作。",
        "There is no debit authorization to cancel; this message executed no operation.",
    ),
    # B1: a purchase is reviewed in chat; the device gesture signs it (wealth page preparer).
    # C1 (R-10): every preference the gesture signs is listed, from the signed snapshot.
    "purchase_review": (
        "请核对申购：{product}，金额 {amount} 元，从{account}扣款；投资期限 {horizon} 天，"
        "取用等待不超过 {access} 天，只投{types}，比较目标：{objective}。点“确认”即在模拟"
        "设备上签署以上申购偏好并生成规范卡；之后还需短信验证（模拟）与最终确认才会办理。"
        "本条消息没有执行任何操作。",
        "Review the purchase: {product}, CNY {amount}, debited from the {account}; horizon "
        "{horizon} days, access within {access} days, {types} only, objective: {objective}. "
        "Confirm signs exactly these preferences on the simulated device and prepares the "
        "canonical card; simulated SMS verification and a final approval follow before "
        "anything executes. This message executed no operation.",
    ),
    "purchase_changed": (
        "申购要素在您核对之后有变化，没有签署任何内容。请重新说明申购要求，核对新的要素后再"
        "确认；本条消息没有执行任何操作。",
        "The purchase terms changed after you reviewed them, so nothing was signed. Restate "
        "the purchase and review the new terms before confirming; this message executed no "
        "operation.",
    ),
    # C1: a sentence that asks, supposes or reports the past orders nothing in chat.
    "mood_hold": (
        "这句话是在询问、假设或说明已经发生的事，我没有办理这项业务，也没有生成卡片；本条消息"
        "没有执行任何操作。如需现在办理，请直接说要办理的业务。",
        "This reads as a question, a supposition or something that already happened, so "
        "nothing was prepared and no card was created; this message executed no operation. "
        "To do it now, say the request directly.",
    ),
    # C1: a time word on a purchase, redemption or cancellation is never read as "now".
    "time_hold_wealth": (
        "对话里还不能预约申购或赎回：您的话里提到了时间，所以没有生成申购要素或赎回卡片；"
        "本条消息没有执行任何操作。如需现在办理，请去掉时间后重新说，或到理财页办理。",
        "Purchases and redemptions cannot be scheduled in chat: your words named a time, so "
        "no purchase review or redemption card was prepared; this message executed no "
        "operation. To do it now, restate it without the time, or use the wealth page.",
    ),
    "time_hold_subscription": (
        "取消订阅在对话里只能马上办理：您的话里提到了时间，所以没有生成取消卡片；本条消息没有"
        "执行任何操作。如需现在取消，请去掉时间后重新说，或到订阅页办理。",
        "A subscription can only be cancelled now in chat: your words named a time, so no "
        "cancellation card was prepared; this message executed no operation. To cancel now, "
        "restate it without the time, or use the subscriptions page.",
    ),
    # S6 (C2): a gesture reached the controller before the two recipient questions passed.
    "scam_gate": (
        "这笔付款须先回答两道自查问题（收款人账号是否经独立渠道核实、是否有人正在指导您转账），"
        "请在对话页或转账页作答；答完之前不会发送验证码，也不会确认或提交这笔付款。",
        "This payment needs the two self-check questions first (was the payee's account "
        "verified through an independent channel, is anyone guiding you right now); answer "
        "them on the chat or transfer page. Until then no verification code is sent and the "
        "payment is neither confirmed nor submitted.",
    ),
    "purchase_product": (
        "您想申购哪一款？可选：{items}。可以直接说产品名称，或在理财页比较后选择；"
        "本条消息没有执行任何操作。",
        "Which product would you like to buy? Options: {items}. Name it here, or compare and "
        "choose on the wealth page; this message executed no operation.",
    ),
    "purchase_amount": (
        "您想申购{product}多少金额？请说一个明确的金额，例如“2000元”；本条消息没有执行任何操作。",
        "How much of {product} would you like to buy? Name an exact amount such as CNY 2,000; "
        "this message executed no operation.",
    ),
    "purchase_both": (
        "您想申购哪一款、多少金额？可选：{items}；本条消息没有执行任何操作。",
        "Which product, and how much? Options: {items}; this message executed no operation.",
    ),
    "purchase_deictic": (
        "“那只 / 这款”指的是理财页上看到的产品，对话里无法确定是哪一款。请直接说产品名称"
        "（可选：{items}），或在理财页选择；本条消息没有执行任何操作。",
        "'That one' refers to a product seen on the wealth page, which the chat cannot "
        "identify. Name the product (options: {items}) or choose it on the wealth page; this "
        "message executed no operation.",
    ),
    # B2: cancelling an existing scheduled transfer: what this message did not do, then
    # what has not happened to the existing authorization, then the choice.
    "schedule_cancel_lead": (
        "本条消息没有发起新付款，也没有生成新卡片。",
        "This message started no new payment and created no new card. ",
    ),
    "schedule_cancel_choose": (
        "已有的定时转账授权尚未取消。您有以下定时转账授权：{items}。请选择要取消的那一项，"
        "我会生成撤销卡片，在模拟设备上确认后显示撤销结果。",
        "Existing scheduled transfer authorizations have not been cancelled. You have: "
        "{items}. Choose the one to cancel; a revocation card follows, and the result is "
        "shown after you confirm it on the simulated device.",
    ),
    "schedule_cancel_none": (
        "您目前没有进行中的定时转账授权。",
        "You have no active scheduled transfer authorization.",
    ),
    "redeem_none": (
        "您目前没有可赎回的持仓；本条消息没有执行任何操作。",
        "You hold no position that can be redeemed; this message executed no operation.",
    ),
    "redeem_no_match": (
        "没有找到您说的那款产品的持仓。您持有：{items}。请说明要赎回哪一笔，或在理财页选择；"
        "本条消息没有执行任何操作。",
        "You hold no position in the product you named. You hold: {items}. Say which one to "
        "redeem, or choose it on the wealth page; this message executed no operation.",
    ),
    "redeem_choose": (
        "您有多笔持仓：{items}。请说明要赎回哪一笔，或在理财页选择；本条消息没有执行任何操作。",
        "You hold several positions: {items}. Say which one to redeem, or choose it on the "
        "wealth page; this message executed no operation.",
    ),
    # C1: a quantity the grammar does not read exactly is asked, never taken as "all".
    "redeem_quantity": (
        "您持有{items}。请说明要赎回的金额，或说“全部赎回”；本条消息没有执行任何操作。",
        "You hold {items}. Say the amount to redeem, or say 'redeem all'; this message "
        "executed no operation.",
    ),
    "redeem_quantity_over": (
        "您说的金额超过了可赎回的数额（{items}）。请说一个不超过它的金额，或说“全部赎回”；"
        "本条消息没有执行任何操作。",
        "That amount is more than can be redeemed ({items}). Name an amount within it, or "
        "say 'redeem all'; this message executed no operation.",
    ),
    # C2: the phrase gate after a receipt for interrupted work says only what is true.
    "scam_phrase_after_receipt": (
        "您这句话提到了「{authority}」，还提到了「{pressure}」。银行、公安和反诈中心不会让您"
        "向“安全账户”转账，也不会因为您不转账就冻结账户。这笔付款没有生成。如需核实，请先"
        "挂断对方电话，拨打 96110 或银行卡背面的官方客服电话。",
        "You mentioned {authority} and also {pressure}. Banks, the police and anti-fraud "
        "centres never ask you to move money to a 'safe account', and never freeze your "
        "account because you did not transfer. This payment was not created. To check, hang "
        "up first, then call 96110 or the official number on the back of your card.",
    ),
}
# C1: the accounts and objectives a purchase review names (display words only).
_ACCOUNT_WORDS = {"A_CURRENT": ("活期账户", "current account")}
_OBJECTIVE_WORDS = {
    "net_return": ("净收益（不保证）", "net return (not guaranteed)"),
    "total_fee": ("总费用", "total fees"),
    "liquidity": ("流动性", "liquidity"),
}
_TYPE_WORDS = {"wealth": ("理财类产品", "wealth products")}
# C1: the S1 interrupt trigger (unchanged since S1): a negated transfer interrupts running
# work and withdraws pending cards. It never fires on a negated cancellation, a standing
# transfer (JUDGING-1 item 19: revocation goes through its card) or a phrase-gate sentence.
_S1_TRIGGER = re.compile(r"(?:不要|先别|别给|不想|先不).{0,30}(?:转|汇款|打钱)")
# C1: words a standalone answer to an unfinished purchase may carry besides the product
# name or the amount (稳健30天持有那款 / 就3000元吧 / 买3000元).
_ANSWER_FILLER = re.compile(
    r"那就|就是|就行|就好|即可|好的|我选|我要|我想要|想要|买入|申购|认购|购买|那款|这款|那只|"
    r"这只|那个|这个|要|选|就|好|嗯|行|买|吧|呀|啊|呢|的|了|元|块|钱|人民币|[\s，,。.!！~～…]"
)
_BARE_AMOUNT = re.compile(r"[0-9]{1,9}(?:\.[0-9]{1,2})?")
# Receipt wording for interrupted or inspected plans, built only from the gateway's node
# statuses so the reply never claims that nothing ran when a node already executed.
_RECEIPT = {
    "interrupted": ("计划已中断。", "Plan interrupted. "),
    "running": ("计划仍在进行。", "The plan is still running. "),
    "plan_n": ("计划{n}：", "Plan {n}: "),
    "executed": ("已执行：{nodes}；", "executed: {nodes}; "),
    "in_flight": ("处理中、以银行回执为准：{nodes}；", "in progress, see bank receipts: {nodes}; "),
    "failed": ("执行失败：{nodes}；", "failed: {nodes}; "),
    "cancelled": ("未执行、已取消：{nodes}。", "not executed and cancelled: {nodes}. "),
    "pending": ("尚未执行：{nodes}。", "not yet executed: {nodes}. "),
    "none": ("无", "none"),
    "retired": ("已撤回{n}张待确认的卡片。", "Withdrew {n} pending card(s). "),
    "wealth": (
        "理财执行的剩余步骤已停止，已执行部分以理财页回执为准。",
        "Remaining wealth execution stopped; see the wealth receipts for executed legs. ",
    ),
    "kept": ("已执行的操作不会自动撤销。", "Executed steps are not undone automatically. "),
    "refund": (
        "如需退回已锁定的资金，可以说“退回锁定的预算”，或在任务面板点“补偿”。",
        "To return locked funds, ask to return the locked budget or use Compensate in the "
        "task panel.",
    ),
    "no_card": ("未生成转账卡片。", "No transfer card was created. "),
}
_NODE_LABELS = {
    "lock_funds": ("锁定预算", "lock budget"),
    "unlock_funds": ("到期解锁预算", "unlock budget when due"),
    "merchant_order": ("商户订购", "merchant order"),
    "transfer": ("转账", "transfer"),
    "schedule_transfer": ("定时转账", "scheduled transfer"),
    "purchase_product": ("理财申购", "product purchase"),
    "redeem_product": ("理财赎回", "product redemption"),
}
_MERCHANT_LABELS = {"FLOWERS": ("订购鲜花", "order flowers"), "CAKE": ("订购蛋糕", "order cake")}
_EXECUTED = frozenset({"succeeded", "compensating", "compensated", "non_reversible"})
_IN_FLIGHT = frozenset({"running", "waiting_settlement", "unknown"})
# Words that cancel the plan itself, so a compensation request may first interrupt it.
# S5: the one list lives with the negation gate, which lets these words through a refund.
_CANCEL_PLAN = PLAN_CANCEL


def _message(key):
    return _MESSAGES[key][0 if get_locale() == "zh-CN" else 1]


def _yuan(value):
    """An exact amount as display text (2000 or 49.90); never a float."""
    fraction = Fraction(value)
    if fraction.denominator == 1:
        return f"{fraction.numerator:,}"
    shown = (Decimal(fraction.numerator) / Decimal(fraction.denominator)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_EVEN
    )
    return f"{shown:,}"


def _receipt_word(key, **values):
    return _RECEIPT[key][0 if get_locale() == "zh-CN" else 1].format(**values)


def _node_label(node):
    index = 0 if get_locale() == "zh-CN" else 1
    merchant = node.get("args", {}).get("merchant_ref")
    if node.get("tool") == "merchant_order" and isinstance(merchant, dict):
        label = _MERCHANT_LABELS.get(str(merchant.get("object_ref")))
        if label:
            return label[index]
    label = _NODE_LABELS.get(str(node.get("tool")))
    return label[index] if label else str(node.get("tool"))


def plan_receipt(snapshot):
    """Group a gateway plan snapshot's nodes by what actually happened to each one."""
    groups: dict[str, list] = {"executed": [], "in_flight": [], "failed": [], "not_executed": []}
    for node in snapshot["plan"]["nodes"]:
        status = snapshot["nodes"][node["node_id"]]["status"]
        group = (
            "executed"
            if status in _EXECUTED
            else "in_flight"
            if status in _IN_FLIGHT
            else "failed"
            if status == "failed"
            else "not_executed"
        )
        groups[group].append({
            "node_id": node["node_id"],
            "tool": node["tool"],
            "label": _node_label(node),
            "status": status,
        })
    return {"plan_id": snapshot["plan"]["plan_id"], "blocked": snapshot["blocked"], **groups}


def receipt_text(receipt, *, refund_hint=True):
    """Render a stop or plan receipt; every executed node is named, never denied."""
    plans = receipt.get("plans", [])
    parts = []
    separator = "、" if get_locale() == "zh-CN" else ", "
    for number, plan in enumerate(plans, start=1):

        def names(group, plan=plan):
            return separator.join(item["label"] for item in plan[group]) or _receipt_word("none")

        text = _receipt_word("interrupted" if plan["blocked"] else "running")
        if len(plans) > 1:
            text = _receipt_word("plan_n", n=number) + text
        text += _receipt_word("executed", nodes=names("executed"))
        if plan["in_flight"]:
            text += _receipt_word("in_flight", nodes=names("in_flight"))
        if plan["failed"]:
            text += _receipt_word("failed", nodes=names("failed"))
        cancelled = plan["blocked"] and all(
            item["status"] == "cancelled" for item in plan["not_executed"]
        )
        text += _receipt_word("cancelled" if cancelled else "pending", nodes=names("not_executed"))
        parts.append(text)
    if receipt.get("retired_drafts"):
        parts.append(_receipt_word("retired", n=receipt["retired_drafts"]))
    if receipt.get("wealth_stopped"):
        parts.append(_receipt_word("wealth"))
    executed = [item for plan in plans for item in plan["executed"]]
    if executed:
        parts.append(_receipt_word("kept"))
        if refund_hint and any(
            item["tool"] == "lock_funds" and item["status"] == "succeeded" for item in executed
        ):
            parts.append(_receipt_word("refund"))
    return "".join(parts).strip() if parts else _message("stop_none")


def _recorded_rules(value):
    if isinstance(value, dict):
        rules = set(value.get("rule_ids", []))
        for child in value.values():
            rules.update(_recorded_rules(child))
        return rules
    if isinstance(value, list):
        return set().union(*(_recorded_rules(child) for child in value))
    return set()


def _billing_period(text, now):
    local_day = business_date(now)
    year, month = int(local_day[:4]), int(local_day[5:7])
    if "上月" in text or "上个月" in text:
        year, month = (year, month - 1) if month > 1 else (year - 1, 12)
        return f"{year:04d}-{month:02d}"
    explicit_year = re.search(r"(?<!\d)(\d{4})年", text)
    if explicit_year:
        year = int(explicit_year.group(1))
    if any(word in text for word in ("年", "年度")):
        return f"{year:04d}"
    explicit_month = re.search(r"(?<!\d)(1[0-2]|[1-9])月", text)
    if explicit_month:
        month = int(explicit_month.group(1))
    elif "九月" in text:
        month = 9
    return f"{year:04d}-{month:02d}"


class DomesticSession:
    """Inject existing controllers; all writes remain behind their device gestures."""

    def __init__(
        self, controller, wealth_controller=None, client=None, *, task_id="DOMESTIC", suggest=False
    ):
        self.controller = controller
        self.wealth_controller = wealth_controller
        self.client = client or controller.client
        self.task_id = task_id
        self.history = []
        self.tool_calls = 0
        self._sequence = 0
        self._requests = {}
        self._responses = {}
        self._modes = {}
        self._previous_intent = None
        self._aa_choices = {}
        # B1: the slots of an unfinished purchase, kept for exactly the next turn.
        self._wealth_pending = None
        self._subscription_choices = {}
        # R2-E4: signed purchase preferences whose pending was withdrawn after the signed
        # terms failed the comparison (kept as a record, not deleted).
        self.withdrawn_inputs = []
        # C1: the planner disagreed with the lexical router on the current turn.
        self._disagreed = False
        # Model suggestions for a request the lexical rules cannot read: questions only,
        # asked only of a live client and only when a page that shows them opts in.
        self.suggestions = domestic_suggest.SuggestionDesk(enabled=suggest)

    def _call(self, operation, *args, **kwargs):
        self.tool_calls += 1
        return operation(*args, **kwargs)

    def _context(self):
        chat = self.controller.chat
        return {
            **trusted_slot_context(self.controller.state, self.controller.device.principal),
            "previous_intent": self._previous_intent,
            "confirmed_contact": chat._previous_contact,
            "confirmed_amount": chat._previous_amount,
            "cancellable": next(
                (
                    response["payload"]["cancellable"]
                    for response in reversed(list(self._responses.values()))
                    if response["status"] == "settled" and "cancellable" in response["payload"]
                ),
                [],
            ),
        }

    def _classify(self, text):
        context = self._context()
        context["contact_text"] = self.controller.gateway.vault.tokenize(text)
        lexical = recognize(text, context)
        if lexical.intent in {"out_of_scope", "password_change", "stop", "handoff", "clarify"}:
            return lexical
        safe = self.controller.gateway.vault.tokenize(text)
        self.controller.gateway.vault.ensure_safe(safe)
        result = self.client.complete(
            task_id=self.task_id,
            step=f"intent:{self._sequence}",
            prompt={
                "instruction": "Classify the banking intent only. Never return amounts, "
                "accounts, products, permissions or executable arguments.",
                "intent_definitions": {
                    "birthday": "Birthday planning, or returning/releasing budget funds locked "
                    "for an existing plan. This includes plan compensation even without birthday "
                    "wording. It does not mean a merchant refund, card unlocking, or a transfer. "
                    "The label grants no permission and does not establish whether a plan exists "
                    "or is stopped; the application checks state and requires device approval.",
                },
                "user_text": safe,
                "response_schema": IntentCandidate.model_json_schema(),
            },
            tool_state={"previous_intent": context["previous_intent"]},
            scripted=lambda: lexical.model_dump(mode="json"),
        )
        from corridor.agent.llm import validate_model_output

        candidate = validate_model_output(self.client, IntentCandidate, result.output)
        # A model cannot redirect an unsupported utterance into an executable tool.
        if candidate.intent != lexical.intent:
            # C1: a clarification that comes from this disagreement never continues an
            # unfinished purchase either (see _receive_business).
            self._disagreed = True
            return IntentCandidate(intent="clarify", needs_clarification=True)
        return candidate

    def _envelope(
        self, identifier, candidate, payload, *, mode="none", message="preview", text=None
    ):
        card = payload.get("card", {})
        route = SCENARIOS[candidate.intent]
        if candidate.intent == "handoff":
            route = "operator"
        elif route in {"control", "sensitive", "out_of_scope", "clarify"}:
            route = "conversation"
        tier = card.get("tier")
        if (
            candidate.intent in {"balance_query", "bill_report"}
            and payload.get("status") == "settled"
        ):
            tier = "green"
        nodes = payload.get("nodes", [])
        if nodes and tier is None:
            tiers = {node.get("card", {}).get("tier") for node in nodes}
            tier = "red" if "red" in tiers else "yellow" if "yellow" in tiers else None
        response = {
            "response_id": identifier,
            "intent": candidate.intent,
            "scenario": SCENARIOS[candidate.intent],
            "route": route,
            "status": payload.get("status", "clarify"),
            "needs_clarification": payload.get("status")
            in {"clarify", "awaiting_device_selection"},
            "tier": tier,
            "requested_level": "red"
            if candidate.intent in {"card_lost", "password_change"}
            else None,
            "card": deepcopy(card),
            "rule_ids": list(payload.get("rule_ids", [])),
            "audit_hash": self.controller.gateway.audit.checkpoint()["head"],
            "payload": deepcopy(payload),
            "message": text or _message(message),
            "simulated": True,
            "planner_mode": self.client.mode,
            "buttons": [],
            "recipient_context": bool(payload.get("recipient_context", False)),
        }
        if mode != "none":
            response["buttons"] = [
                {"id": "confirm", "response_id": identifier, "requires_device": True}
            ]
        self._responses[identifier] = response
        self._modes[identifier] = mode
        return deepcopy(response)

    def receive(self, text: str, *, request_id: str | None = None, external_files: int = 0):
        # SG-6: the words (``alarm``) and the count of external files that came with them
        # are read before anything else, so a turn whose processing raises, or that carries
        # an external file, still raises the desk's context flag; a raised flag offers
        # nothing, from this turn on. Only a page passes ``external_files`` (the count, never
        # a file's content); every other caller keeps the default 0.
        domestic_suggest.observe(self, text, external_files=external_files)
        if not isinstance(text, str) or not text.strip() or len(text) > 4000:
            raise ValueError("A nonempty banking request of at most 4000 characters is required")
        digest = self.controller.gateway.utterance_digest(text, None)
        if request_id is not None and request_id in self._requests:
            old_digest, identifier = self._requests[request_id]
            if old_digest != digest:
                raise ValueError("Request ID cannot replace an existing utterance")
            return deepcopy(self._responses[identifier])
        self._sequence += 1
        identifier = stable_id(self.task_id, self._sequence, "conversation-response")
        request_id = request_id or identifier
        # B1: an unfinished purchase lives for exactly one more turn.
        pending, self._wealth_pending = self._wealth_pending, None
        context = {**self._context(), "contact_text": self.controller.gateway.vault.tokenize(text)}
        business = False
        response = None
        try:
            lexical = recognize(text, context)
            if lexical.intent == "out_of_scope":
                response = self._envelope(
                    identifier, lexical, {"status": "refused"}, message="refused"
                )
            elif lexical.intent == "password_change":
                response = self._envelope(
                    identifier, lexical, {"status": "refused"}, message="password"
                )
            elif not self.controller.snapshot()["logged_in"]:
                response = self._envelope(
                    identifier, lexical, {"status": "login_required"}, message="login"
                )
            else:
                self._disagreed = False
                candidate = self._classify(text)
                response = self._receive_business(
                    identifier,
                    candidate,
                    text,
                    request_id,
                    pending=None if self._disagreed else pending,
                )
                business = True
        finally:
            # SG-2: every received turn may raise the desk's context flag (it never comes
            # down); a raised flag offers nothing from this turn on. SG-6: read also when
            # the processing above raised (then without a reply: the words only).
            domestic_suggest.observe(self, text, response)
        if business:
            response = domestic_suggest.offer(self, identifier, lexical, text, response)
        self._previous_intent = response["intent"]
        self._requests[request_id] = (digest, identifier)
        self.history.append({
            "utterance_hash": digest,
            "response_id": identifier,
            "intent": response["intent"],
            "status": response["status"],
        })
        return response

    def _receive_business(self, identifier, candidate, text, request_id, *, pending=None):
        intent = candidate.intent
        stop_negated = False
        if intent == "stop" and (cancel_negated(text) or standing_transfer(text)):
            # C5 (fix step; Opus review 1, external review 15): a stop phrase (不转了, 别转了,
            # 停止转账, 取消这笔转账) in a sentence that negates the cancellation itself (先别
            # 取消这笔转账) or names a standing transfer (每月给房东的转账不转了) is answered
            # as the same sentence without the stop wording: the S5 reply, or the standing
            # authorization listed for its own revocation card. It interrupts nothing.
            stop_negated = cancel_negated(text)
            candidate = IntentCandidate(intent="clarify", needs_clarification=True)
            intent = "clarify"
        payload, mode, message = {"status": "clarify"}, "none", "clarify"
        reply = None
        continued = None
        # S6 R7: the phrase gate held it before any planner call (no draft, no card).
        scam = scam_phrase_hold(text, self._context()) if intent == "clarify" else None
        if (
            intent == "clarify"
            and _S1_TRIGGER.search(text)
            and not cancel_negated(text)
            and not standing_transfer(text)
            and not scam
        ):
            # S1: a negated transfer interrupts running work and withdraws pending cards.
            # C1: an established standing authorization is never revoked here: it is
            # revoked only through its own card (list -> yellow card -> device confirm).
            receipt = self._stop_receipt(self._interrupt(keep_standing=True))
            self.withdraw_pending()
            if receipt["plans"] or receipt["retired_drafts"]:
                # The negation also interrupted running work; say exactly what that was.
                payload = {"status": "clarify", "stop_receipt": receipt}
                reply = _receipt_word("no_card") + receipt_text(receipt)
        # S5: the shared negation gate held a money-moving request. Only the trigger above
        # (unchanged since S1, narrowed in C1) interrupts running work; the gate itself
        # changes nothing.
        held = negation_hold(text, self._context()) if intent == "clarify" else ()
        if not held and stop_negated:
            # C5: "do not cancel" said with stop wording gets the same S5 reply.
            held = negation_words(text)
        if held:
            payload = {**payload, "negation": {"words": list(dict.fromkeys(held))}}
            message = "negated"
        if scam:
            payload = {**payload, "scam_phrase": deepcopy(scam)}
            message = "scam_phrase"
            separator = "、" if get_locale() == "zh-CN" else ", "
            # C2: after a receipt for interrupted work the reply never says that nothing
            # was executed (the S1 trigger no longer fires with the gate; kept for safety).
            reply = (reply or "") + _message(
                "scam_phrase_after_receipt" if reply else "scam_phrase"
            ).format(
                authority=separator.join(scam["authority"]),
                pressure=separator.join(scam["pressure"]),
            )
        elif intent == "clarify" and standing_cancel(text):
            # B2 (JUDGING-1 list item 19): asking to cancel an existing standing transfer is
            # answered in two separate statements: no new payment or card came from this
            # message, and the existing authorization has not been cancelled. The customer's
            # own active authorizations are then listed; a choice prepares the revocation.
            # C1: "no longer monthly" (不想每月…转了) asks for this too; "do not cancel"
            # (不要取消, 别停) never does and keeps the S5 reply above.
            standing = self._standing_authorizations()
            payload = {
                **payload,
                "schedule_cancel": {
                    "status": "choose" if standing else "none",
                    "candidates": [row["ref"] for row in standing],
                },
            }
            message = "schedule_cancel"
            rest = (
                _message("schedule_cancel_choose").format(items=self._standing_items(standing))
                if standing
                else _message("schedule_cancel_none")
            )
            # A receipt for work the S1 trigger interrupted stays in front of it.
            reply = (reply or "") + _message("schedule_cancel_lead") + rest
        if intent in {"clarify", "investment"} and pending and not held and not scam:
            # B1: the next turn may supply the product or amount an unfinished purchase
            # asked for (稳健30天持有那款 / 3000元). C1: only a standalone answer to the
            # asked slot continues it; anything else (another business, a question, a
            # negation, a new purchase naming something else) drops that purchase.
            answer = self._slot_answer(text, pending)
            if answer is not None:
                continued = self._purchase(text, pending, answer=answer)
                candidate = IntentCandidate(
                    intent="investment", needs_clarification=continued["mode"] == "none"
                )
        if continued is not None:
            payload, mode, message, reply = (
                continued["payload"],
                continued["mode"],
                continued["message"],
                continued["reply"],
            )
            self._wealth_pending = continued["pending"]
        elif intent == "transfer":
            context = {
                **self._context(),
                "contact_text": self.controller.gateway.vault.tokenize(text),
            }
            slots = extract_slots(text, context, intent=intent)
            schedule = (
                {"status": "none"} if "aa" in text.lower() else schedule_candidate(text, context)
            )
            if (
                slots["parameters"]["date"]
                or has_date_request(text)
                or "aa" in text.lower()
                or schedule["status"] != "none"
            ):
                # Dated or split-bill text yields the form's own card or none at all;
                # it never falls back to an immediate transfer preview.
                payload, mode, message = self._direct_card(
                    text, candidate, context, slots, schedule, request_id
                )
            elif slots["parameters"]["amount"] is None:
                payload = {"status": "clarify", **slots}
            elif candidate.needs_clarification and slots["parameters"]["recipient"]:
                payload = {"status": "clarify", **slots}
            else:
                payload = self._call(
                    self.controller.preview_transfer,
                    normalized_transfer(text),
                    request_id=request_id,
                )
                mode = "transfer" if payload.get("card") else "none"
                message = "preview" if mode != "none" else "clarify"
                payload.update(slots)
        elif intent == "balance_query":
            payload = self._call(self.controller.query_balance)
            message = "result"
        elif intent == "bill_report" and history_listing(text):
            # The same green transaction-history query as the billing page's button.
            payload = self._call(self.controller.query_transactions)
            message = "result"
        elif intent == "bill_report":
            period = _billing_period(text, self.controller.state.now)
            query = (
                self.controller.anomaly_query
                if any(word in text for word in ("不认识", "异常", "可疑", "重复扣款"))
                else self.controller.bill_report
            )
            payload = self._call(query, period)
            message = "result"
        elif intent == "investment" and wealth_action(text) == "redeem":
            # B1: one of the customer's own holdings, through the redemption form's preparer.
            payload, mode, message, reply = self._redeem(text, request_id)
        elif intent == "investment" and wealth_action(text) == "purchase":
            # B1: product and amount slots filled in chat; a complete purchase is reviewed
            # here and signed only by the device gesture (see confirm). C1: a new purchase
            # request starts afresh; it never inherits the unfinished one's product.
            attempt = self._purchase(text, None)
            payload, mode, message, reply = (
                attempt["payload"],
                attempt["mode"],
                attempt["message"],
                attempt["reply"],
            )
            self._wealth_pending = attempt["pending"]
            if mode == "none" and self.wealth_controller is not None:
                # Unchanged for an unfinished request: the wealth page's dialogue hears it.
                payload["dialogue"] = self._call(
                    self.wealth_controller.receive_goal, text, request_id=request_id
                )
        elif intent == "investment":
            payload = {"status": "awaiting_device_selection"}
            if self.wealth_controller is not None:
                payload["dialogue"] = self._call(
                    self.wealth_controller.receive_goal, text, request_id=request_id
                )
            message = "wealth"
        elif intent in {"card_apply", "card_lost", "card_freeze"}:
            cards = [
                card.id
                for card in self.controller.state.cards.values()
                if card.owner == self.controller.device.principal
            ]
            if intent == "card_apply" or len(cards) == 1:
                kind = {
                    "card_apply": "apply_virtual_card",
                    "card_lost": "report_card_lost",
                    "card_freeze": "freeze_card",
                }[intent]
                payload = self._call(
                    self.controller.preview_card_operation,
                    kind,
                    card_ref=cards[0] if intent != "card_apply" else None,
                    request_id=request_id,
                )
                mode, message = "pending", "preview"
            else:
                payload, message = {"status": "awaiting_device_selection"}, "device"
        elif intent in {"card_limit", "card_restriction", "card_unlock"}:
            payload, message = {"status": "awaiting_device_selection"}, "device"
        elif intent == "subscription_query":
            payload = self._call(self.controller.preview_subscriptions, request_id=request_id)
            mode, message = "pending", "preview"
        elif intent == "subscription_cancel":
            payload, mode, message, reply = self._subscription_cancel(text, request_id)
        elif intent == "birthday" and compensation_request(text):
            payload, mode, message, reply = self._compensation(text)
        elif intent == "birthday":
            # Only a closed relation vocabulary narrows trusted calendar events. Dates
            # written in the request never select an event or change a derived date.
            payload = self._call(
                self.controller.preview_birthday,
                request_id=request_id,
                relation=relation_in_text(text),
            )
            if payload.get("status") == "draft":
                mode, message = "plan", "preview"
            else:
                payload = {**payload, "status": "clarify", "birthday_status": payload["status"]}
                message = (
                    "birthday_window"
                    if payload["birthday_status"] == "outside_lock_month"
                    else "birthday_select"
                )
        elif intent == "stop":
            # C5: only a bare stop (停止, 不转了, 等一下 ...) keeps the S1 meaning of
            # interrupting every registered plan. A stop phrase inside a sentence naming
            # anything else (房东那边先不转了, 取消这笔转账) interrupts like the S1 negation
            # trigger: an established standing authorization is left to its own card.
            keep = not natural_stop(text)
            payload = self._interrupt(keep_standing=keep)
            receipt = self._stop_receipt(payload)
            if not receipt["plans"] and not receipt["retired_drafts"]:
                # C5: the status matches the receipt (a finished card is not withdrawn).
                payload = {**payload, "status": "no_active_plan"}
            if self.wealth_controller is not None:
                wealth = self._call(self.wealth_controller.stop)
                receipt["wealth_stopped"] = bool(wealth.get("execution"))
                payload = self._control_payload(payload, wealth)
            self.withdraw_pending()
            payload["stop_receipt"] = receipt
            message, reply = "result", receipt_text(receipt)
            if keep and self._standing_authorizations():
                reply += _message("stop_kept_standing")
        elif intent == "handoff":
            # Retire unsigned drafts as well as registered plans before handing over.
            self._call(self.controller.stop)
            payload = self._call(self.controller.handoff)
            if self.wealth_controller is not None:
                handed_off = None
                if self.wealth_controller.parent_id is not None:
                    handed_off = self._call(self.wealth_controller.keep_or_handoff, handoff=True)
                # The terminal handoff control is retained while UI proposals are cleared.
                wealth = self._call(self.wealth_controller.stop)
                if handed_off is not None:
                    wealth["handoff"] = handed_off
                payload = self._control_payload(payload, wealth)
            self.withdraw_pending()
            payload["session_snapshot"] = [
                {**row, "status": self._responses[row["response_id"]]["status"]}
                for row in self.history
            ]
            payload["dag_snapshot"] = self.controller.snapshot()["plans"]
            message = "result"
        return self._envelope(
            identifier, candidate, payload, mode=mode, message=message, text=reply
        )

    def _interrupt(self, *, keep_standing=False):
        """Controller Stop, keeping how many unsigned cards it retired alongside any plans.

        ``keep_standing`` (C1, the S1 negation trigger) leaves established standing
        transfer authorizations running: only their own revocation card revokes them.
        """
        # C1 / C5: count only the cards that were still open; a finished action is not
        # "withdrawn" by this message (an explicit stop included).
        opened = getattr(self.controller, "open_actions", None)
        retired = (
            len(opened())
            if callable(opened)
            else len(self.controller._drafts) + len(self.controller._pending)
        )
        if not keep_standing:
            payload = self._call(self.controller.stop)
            return {**payload, "retired_drafts": retired}
        payload = self._call(self.controller.stop, keep_standing=True)
        return {**payload, "retired_drafts": retired}

    def _stop_receipt(self, payload):
        plans = []
        for plan_id in payload.get("results", {}):
            try:
                plans.append(plan_receipt(self.controller.gateway.plan_snapshot(plan_id)))
            except (KeyError, ValueError):
                continue
        return {"plans": plans, "retired_drafts": payload.get("retired_drafts", 0)}

    def _compensation(self, text):
        """Route a refund request to the task panel's own compensation preparer.

        The latest registered plan is the one the task panel's Compensate button uses. A
        running plan is interrupted first only when the user also cancels it; otherwise the
        reply lists its node states and asks for an explicit stop. Nothing is signed here.
        """
        plans = self.controller._registered_plan_ids()
        if not plans:
            return {"status": "no_active_plan"}, "none", "compensation_no_plan", None
        plan_id = plans[-1]
        current = plan_receipt(self.controller.gateway.plan_snapshot(plan_id))
        stopped = None
        if not current["blocked"] and (current["not_executed"] or current["in_flight"]):
            if not _CANCEL_PLAN.search(text):
                receipt = {"plans": [current], "retired_drafts": 0}
                return (
                    {"status": "clarify", "compensation": {"plan_id": plan_id}, "plan": current},
                    "none",
                    "compensation_active",
                    receipt_text(receipt, refund_hint=False) + _message("compensation_active"),
                )
            self._call(self.controller.stop, plan_id)
            stopped = {
                "plans": [plan_receipt(self.controller.gateway.plan_snapshot(plan_id))],
                "retired_drafts": 0,
            }
        prefix = receipt_text(stopped, refund_hint=False) if stopped else ""
        try:
            result = self._call(self.controller.compensate, plan_id)
        except (ValueError, KeyError, RuntimeError) as error:
            payload = {
                "status": "clarify",
                "compensation": {"plan_id": plan_id},
                "prepare_error": type(error).__name__,
            }
            if stopped:
                payload["stop_receipt"] = stopped
            return payload, "none", "compensation_error", prefix + _message("compensation_error")
        payload = {**result, "compensation": {"plan_id": plan_id}}
        if stopped:
            payload["stop_receipt"] = stopped
        if result.get("pending_id") and result.get("card"):
            return payload, "pending", "compensation_card", prefix + _message("compensation_card")
        return payload, "none", "compensation_none", prefix + _message("compensation_none")

    def _direct_card(self, text, candidate, context, slots, schedule, request_id):
        """Call the business form's own preparer with closed-grammar explicit fields."""
        parameters = slots["parameters"]
        if "aa" in text.lower():
            aa = aa_candidate(text, context)
            if aa["status"] == "complete" and not candidate.needs_clarification:
                expected = {
                    "kind": "aa_request",
                    "participant_refs": aa["participants"],
                    "amount": aa["collect_total"],
                }
                return self._prepared(
                    lambda: self.controller.preview_aa_collection(
                        aa["collect_total"], aa["participants"], request_id=request_id
                    ),
                    expected,
                    {**slots, "aa_parse": aa},
                    request_id,
                )
            return (
                {"status": "awaiting_device_selection", **slots, "aa_parse": aa},
                "none",
                "device",
            )
        if (
            schedule["status"] == "complete"
            and parameters["recipient"]
            and parameters["amount"]
            and not candidate.needs_clarification
        ):
            monthly = schedule["monthly_day"]
            expected = {
                "kind": "schedule_transfer",
                "contact_ref": parameters["recipient"],
                "amount": parameters["amount"],
                "schedule": {"type": "monthly", "day": monthly} if monthly else {"type": "once"},
                "max_runs": 3 if monthly else 1,
                "start": add_seconds(f"{schedule['date_text']}T00:00:00Z", -8 * 3600),
            }
            # C2: the recipient check grades this payment on the user's own words even if a
            # gesture reaches the controller before any page drew the questions.
            remember = getattr(self.controller, "remember_words", None)
            if callable(remember):
                remember(request_id, text)
            return self._prepared(
                lambda: self.controller.preview_scheduled_transfer(
                    None,
                    parameters["recipient"],
                    parameters["amount"],
                    request_id,
                    date_text=schedule["date_text"],
                    monthly_day=monthly,
                ),
                expected,
                {**slots, "schedule_echo": schedule},
                request_id,
            )
        if schedule.get("reason") in {"residual_time", "unparsed_time", "number_not_date"}:
            # A time word outside the grammar, or a number that is not a date, is asked
            # about in chat ("when?"); it is never read as "now". B2 (JUDGING-1 list item
            # 19): an unparsed date word (今晚, 开完会之后) is asked about the same way.
            return (
                {"status": "clarify", **slots, "schedule_parse": schedule},
                "none",
                "number_not_date" if schedule["reason"] == "number_not_date" else "when",
            )
        return (
            {"status": "awaiting_device_selection", **slots, "schedule_parse": schedule},
            "none",
            "device",
        )

    def _prepared(self, preview, expected, extra, request_id):
        """A preparer error clarifies; a card that differs from the parse is discarded."""
        try:
            payload = self._call(preview)
        except (ValueError, KeyError) as error:
            return (
                {"status": "clarify", **extra, "prepare_error": type(error).__name__},
                "none",
                "clarify",
            )
        card = payload.get("card") or {}
        objects = card.get("objects") or {}
        spa = card.get("spa") or {}
        window = card.get("execution_window") or {}
        if expected["kind"] == "aa_request":
            facts = {"participant_refs": objects.get("participant_refs")}
        elif expected["kind"] == "cancel_subscription":
            # B1: the card must cancel exactly the authorization the chat resolved.
            facts = {"authorization_ref": objects.get("authorization_ref")}
        elif expected["kind"] == "redeem_product":
            facts = {"position_ref": objects.get("position_ref")}
        elif expected["kind"] == "cancel_schedule":
            # B2: the revocation card must name exactly the authorization that was chosen.
            facts = {"schedule_ref": objects.get("schedule_ref")}
        else:
            facts = {
                "contact_ref": objects.get("contact_ref"),
                "schedule": spa.get("schedule"),
                "max_runs": spa.get("max_runs"),
                "start": spa.get("start") if spa.get("start") == window.get("start") else None,
            }
        observed = {"kind": card.get("kind"), "amount": card.get("amount"), **facts}
        if observed != expected:
            # Retire only this unsigned draft; other drafts and registered plans stay as
            # they are (Stop would interrupt every plan the controller has registered).
            self.controller._pending.pop(payload.get("pending_id"), None)
            self.controller._requests.pop(request_id, None)
            return {"status": "clarify", **extra, "echo_mismatch": True}, "none", "card_mismatch"
        return {**payload, **extra}, "pending", "preview"

    # --- B1: subscription cancellation, purchase and redemption completed in chat -------

    def _vocabulary(self):
        """Synthetic display names of products and merchants (empty if not provided)."""
        provider = getattr(self.controller, "display_vocabulary", None)
        vocabulary = provider() if callable(provider) else {}
        return {
            "products": dict(vocabulary.get("products", {})),
            "merchants": dict(vocabulary.get("merchants", {})),
        }

    @staticmethod
    def _shown_name(names, ref):
        entry = names.get(ref) or {}
        return entry.get(get_locale()) or entry.get("zh-CN") or entry.get("en") or str(ref)

    def _active_subscriptions(self):
        return [
            {
                "ref": item.id,
                "merchant_ref": item.merchant_ref,
                "amount": item.amount,
                "period_months": item.period_months,
            }
            for item in sorted(self.controller.state.subscriptions.values(), key=lambda row: row.id)
            if item.owner == self.controller.device.principal and item.status == "active"
        ]

    def _ordinal(self, collection, ref, owner_field):
        """C1 (R-10): the 1-based position of an object among its owner's, by identifier
        (the same ordinal the card's object row and the choice buttons show)."""
        items = getattr(self.controller.state, collection)
        item = items.get(ref)
        if item is None:
            return None
        owner = getattr(item, owner_field)
        owned = sorted(key for key, value in items.items() if getattr(value, owner_field) == owner)
        return owned.index(ref) + 1

    def _subscription_items(self, refs, active, vocabulary):
        rows = {row["ref"]: row for row in active}
        zh = get_locale() == "zh-CN"
        items = []
        for ref in refs:
            row = rows.get(ref)
            if row is None:
                continue
            merchant = self._shown_name(vocabulary["merchants"], row["merchant_ref"])
            yearly = row["period_months"] == 12
            amount = _yuan(row["amount"])
            index = self._ordinal("subscriptions", ref, "owner")
            items.append(
                f"{merchant}，{'每年' if yearly else '每月'} {amount} 元（第 {index} 项授权）"
                if zh
                else f"{merchant}, CNY {amount} {'yearly' if yearly else 'monthly'} "
                f"(authorization {index})"
            )
        return ("；" if zh else "; ").join(items)

    def _displayed_subscriptions(self):
        """The authorizations the reply right before this request displayed (C1: never an
        older listing; any reply in between ends what a "那个" can point back at)."""
        if not self._responses:
            return []
        response = list(self._responses.values())[-1]
        if response["intent"] == "subscription_query" and response["status"] == "settled":
            return list(response["payload"].get("cancellable", []))
        if response["intent"] == "subscription_cancel" and response["status"] == "clarify":
            return list(response["payload"].get("subscription_choices", []))
        return []

    def _subscription_cancel(self, text, request_id):
        """One of the customer's own active authorizations, named by the user's words."""
        # C1: a question, supposition, past statement or time word cancels nothing.
        kind = mood(text)
        if kind is not None:
            return (
                {
                    "status": "clarify",
                    "mood_hold": {"kind": kind, "service": "subscription_cancel"},
                },
                "none",
                "mood_hold",
                None,
            )
        if names_a_time(text):
            return (
                {"status": "clarify", "time_hold": {"service": "subscription_cancel"}},
                "none",
                "time_hold_subscription",
                None,
            )
        active = self._active_subscriptions()
        vocabulary = self._vocabulary()
        resolution = cancel_candidates(
            text,
            active,
            shown=self._displayed_subscriptions(),
            merchant_names={
                ref: list(entry.values()) for ref, entry in vocabulary["merchants"].items()
            },
        )
        if resolution["status"] == "resolved":
            ref = resolution["refs"][0]
            payload, mode, message = self._prepared(
                lambda: self.controller.cancel_subscription(ref, request_id=request_id),
                {"kind": "cancel_subscription", "amount": None, "authorization_ref": ref},
                {"subscription_parse": deepcopy(resolution)},
                request_id,
            )
            return payload, mode, message, None
        choices = [] if resolution["status"] == "none" else list(resolution["refs"])
        key = {"none": "subscription_none", "no_match": "subscription_no_match"}.get(
            resolution["status"], "subscription_choose"
        )
        payload = {
            "status": "clarify",
            "subscription_parse": deepcopy(resolution),
            "subscription_choices": choices,
        }
        items = self._subscription_items(choices, active, vocabulary)
        return payload, "none", key, _message(key).format(items=items)

    def prepare_subscription_cancel(self, response_id, ref, *, request_id=None):
        """Choose one listed authorization in chat; its form's own preparer builds the card.

        The listing reply is never rewritten: the card comes under a new reply, and only
        the device gesture signs it.
        """
        if response_id not in self._responses:
            raise ValueError("Unknown response identity")
        source = self._responses[response_id]
        if (
            source["intent"] != "subscription_cancel"
            or source["status"] != "clarify"
            or ref not in source["payload"].get("subscription_choices", [])
            or ref not in {row["ref"] for row in self._active_subscriptions()}
        ):
            raise ValueError("This response lists no such cancellable authorization")
        key = (response_id, ref)
        if key in self._subscription_choices:
            return deepcopy(self._responses[self._subscription_choices[key]])
        self._sequence += 1
        identifier = stable_id(self.task_id, self._sequence, "conversation-response")
        request_id = request_id or identifier
        payload, mode, message = self._prepared(
            lambda: self.controller.cancel_subscription(ref, request_id=request_id),
            {"kind": "cancel_subscription", "amount": None, "authorization_ref": ref},
            {"subscription_parse": {"status": "chosen", "refs": [ref], "clue": "chosen"}},
            request_id,
        )
        response = self._envelope(
            identifier,
            IntentCandidate(intent="subscription_cancel", needs_clarification=False),
            payload,
            mode=mode,
            message=message,
        )
        self._subscription_choices[key] = identifier
        origin = next(row for row in self.history if row["response_id"] == response_id)
        self.history.append({
            "utterance_hash": origin["utterance_hash"],
            "response_id": identifier,
            "intent": response["intent"],
            "status": response["status"],
        })
        # SG-2: a chosen reply's card may carry the S6 warning too.
        domestic_suggest.observe(self, None, response)
        return response

    # --- B2: revoking an existing scheduled transfer authorization -----------------------

    def _standing_authorizations(self):
        """The customer's own scheduled payment authorizations that still have runs ahead."""
        state = self.controller.state
        principal = self.controller.device.principal
        open_runs = {}
        for task in state.schedules.values():
            if task.status in {"pending", "due", "waiting_mfa"}:
                open_runs[task.spa_ref] = open_runs.get(task.spa_ref, 0) + 1
        rows = []
        for item in sorted(state.authorizations.values(), key=lambda row: row.id):
            if (
                getattr(item, "authorization_type", None) != "spa"
                or item.principal != principal
                or item.id not in open_runs
            ):
                continue
            rows.append({
                "ref": item.id,
                "contact_ref": getattr(item.target, "contact_ref", None),
                "amount": item.amount,
                "day": getattr(item.schedule, "day", None),
                "start": item.start,
                "runs": open_runs[item.id],
            })
        return rows

    def _contact_word(self, ref):
        """A trusted alias of the contact (the first Chinese one in zh), else its id."""
        contact = self.controller.state.contacts.get(str(ref))
        aliases = [alias for alias in getattr(contact, "aliases", []) if alias]
        if get_locale() == "zh-CN":
            chinese = [alias for alias in aliases if re.search(r"[一-鿿]", alias)]
            aliases = chinese or aliases
        return aliases[0] if aliases else str(ref)

    def _standing_items(self, rows):
        zh = get_locale() == "zh-CN"
        items = []
        spas = sorted(
            key
            for key, value in self.controller.state.authorizations.items()
            if getattr(value, "authorization_type", None) == "spa"
            and value.principal == self.controller.device.principal
        )
        for row in rows:
            who = self._contact_word(row["contact_ref"]) if row["contact_ref"] else "—"
            amount = _yuan(row["amount"])
            index = spas.index(row["ref"]) + 1 if row["ref"] in spas else "?"
            if zh:
                period = (
                    f"每月{row['day']}号"
                    if row["day"]
                    else f"单次（{business_date(row['start'])}）"
                )
                items.append(
                    f"{who}，{period} {amount} 元（第 {index} 项授权，尚余 {row['runs']} 次）"
                )
            else:
                period = (
                    f"monthly on day {row['day']}"
                    if row["day"]
                    else f"once ({business_date(row['start'])})"
                )
                items.append(
                    f"{who}, {period}, CNY {amount} (authorization {index}, "
                    f"{row['runs']} run(s) ahead)"
                )
        return ("；" if zh else "; ").join(items)

    def prepare_schedule_cancel(self, response_id, ref, *, request_id=None):
        """Choose one listed authorization; the existing cancel_schedule action revokes it.

        The listing reply is never rewritten. The revocation card comes under a new reply
        from the gateway's own preview, and only the device gesture revokes anything.
        """
        if response_id not in self._responses:
            raise ValueError("Unknown response identity")
        source = self._responses[response_id]
        listed = (source["payload"].get("schedule_cancel") or {}).get("candidates", [])
        key = ("schedule", response_id, ref)
        # The current standing authorizations are checked before the cache (R2-E15): a
        # revoked or finished authorization is refused, never answered with an old card.
        if (
            source["status"] != "clarify"
            or ref not in listed
            or ref not in {row["ref"] for row in self._standing_authorizations()}
        ):
            raise ValueError("This response lists no such scheduled authorization")
        if key in self._subscription_choices:
            # The same choice again returns the card it prepared (idempotent per choice).
            return deepcopy(self._responses[self._subscription_choices[key]])
        self._sequence += 1
        identifier = stable_id(self.task_id, self._sequence, "conversation-response")
        request_id = request_id or identifier
        payload, mode, message = self._prepared(
            lambda: self.controller.cancel_schedule(ref, request_id=request_id),
            {"kind": "cancel_schedule", "amount": None, "schedule_ref": ref},
            {"schedule_cancel_choice": {"ref": ref, "source_response": response_id}},
            request_id,
        )
        response = self._envelope(
            identifier,
            IntentCandidate(intent="transfer", needs_clarification=False),
            payload,
            mode=mode,
            message=message,
        )
        self._subscription_choices[key] = identifier
        origin = next(row for row in self.history if row["response_id"] == response_id)
        self.history.append({
            "utterance_hash": origin["utterance_hash"],
            "response_id": identifier,
            "intent": response["intent"],
            "status": response["status"],
        })
        # SG-2: a chosen reply's card may carry the S6 warning too.
        domestic_suggest.observe(self, None, response)
        return response

    def _product_rows(self):
        return [
            {"ref": product.product_id, "min_hold_days": product.min_hold_days}
            for product in self.controller.state.catalog.products
        ]

    def _product_names(self):
        vocabulary = self._vocabulary()
        return {ref: list(entry.values()) for ref, entry in vocabulary["products"].items()}

    def _quantity_text(self, text):
        """The words without product names, identifiers and holding periods (C1)."""
        rows = self._product_rows()
        return without_products(text, self._product_names(), [row["ref"] for row in rows])

    def _slot_answer(self, text, pending):
        """C1: what a standalone answer gives an unfinished purchase, or None.

        Only a turn that is nothing but a product name or an amount (and closed filler
        words) answers it, and only a slot that was asked: no other business word, no
        question, supposition, time or negation, and nothing that conflicts with a slot
        already filled. Anything else drops the unfinished purchase.
        """
        if not pending or pending.get("kind") != "purchase":
            return None
        if negation_words(text) or mood(text) or names_a_time(text):
            return None
        resolution = product_candidates(text, self._product_rows(), self._product_names())
        rest = self._quantity_text(text)
        span = money_span(rest)
        amount = amount_candidate(rest)
        if span is not None:
            rest = rest[: span[0]] + " " + rest[span[1] :]
        rest = _ANSWER_FILLER.sub("", rest)
        if amount is None and _BARE_AMOUNT.fullmatch(rest):
            value = Fraction(rest)
            amount = (
                fraction_string(value) if value > 0 and (value * 100).denominator == 1 else None
            )
            rest = "" if amount is not None else rest
        if rest:
            return None
        answer = {}
        if resolution["status"] == "resolved":
            answer["product_ref"] = resolution["ref"]
        if amount is not None:
            answer["amount"] = amount
        asked = {
            slot
            for slot, field in (("product_ref", "product_ref"), ("amount", "amount"))
            if not pending.get(field)
        }
        if not answer or not set(answer) & asked:
            return None
        for slot, value in answer.items():
            if pending.get(slot) and pending[slot] != value:
                return None
        return answer

    def purchase_review_terms(self, product, amount, objective):
        """The preferences the confirm gesture would sign for this purchase (C1, R-10)."""
        provider = getattr(self.controller, "purchase_terms", None)
        if not callable(provider):
            return None
        return provider(product, amount=amount, objective=objective)

    def _purchase(self, text, pending=None, *, answer=None):
        """Fill the product and amount slots; a complete purchase is shown for review."""
        vocabulary = self._vocabulary()
        if answer is None:
            # C1: a fresh purchase request that asks, supposes, reports the past or names
            # a time is answered in chat: no review, nothing to sign.
            held = self._wealth_hold(text, "purchase")
            if held is not None:
                return held
            resolution = product_candidates(text, self._product_rows(), self._product_names())
            product = resolution["ref"]
            # C1: a number inside a product name is never the amount the user asked for.
            amount = amount_candidate(self._quantity_text(text))
        else:
            resolution = product_candidates(text, self._product_rows(), self._product_names())
            product, amount = answer.get("product_ref"), answer.get("amount")
        progressed = bool(product or amount)
        if pending and pending.get("kind") == "purchase":
            product = product or pending.get("product_ref")
            amount = amount or pending.get("amount")
        missing = [slot for slot, value in (("product", product), ("amount", amount)) if not value]
        shown = "、" if get_locale() == "zh-CN" else ", "
        if missing:
            candidates = list(resolution["refs"])
            parse = {
                "status": "unresolved",
                "missing": missing,
                "product_ref": product,
                "amount": amount,
                "product": "resolved" if product else resolution["status"],
                "candidates": [] if product else candidates,
            }
            key = (
                "purchase_amount"
                if missing == ["amount"]
                else "purchase_deictic"
                if resolution["status"] == "deictic"
                else "purchase_product"
                if missing == ["product"]
                else "purchase_both"
            )
            items = shown.join(self._shown_name(vocabulary["products"], ref) for ref in candidates)
            reply = _message(key).format(
                items=items,
                product=self._shown_name(vocabulary["products"], product) if product else "",
            )
            return {
                "payload": {"status": "awaiting_device_selection", "purchase_parse": parse},
                "mode": "none",
                "message": key,
                "reply": reply,
                "progressed": progressed,
                "pending": {"kind": "purchase", "product_ref": product, "amount": amount},
            }
        purchase = {
            "product_ref": product,
            "amount": amount,
            # Chat never chooses between funding accounts; the wealth page's preparer
            # debits the current account, and its default objective applies.
            "source_ref": "A_CURRENT",
            "objective": getattr(self.controller, "_objective", "net_return"),
        }
        # C1 (R-10): the exact preferences the gesture would sign, shown in full and bound
        # to the confirmation (a change after review is never signed).
        terms = self.purchase_review_terms(product, amount, purchase["objective"])
        payload = {"status": "awaiting_input", "canonical": False, "purchase": purchase}
        if terms is not None:
            payload["purchase_terms"] = deepcopy(terms)
            purchase["source_ref"] = terms.get("source_account_ref", purchase["source_ref"])
        reply = _message("purchase_review").format(**self._terms_words(product, amount, terms))
        return {
            "payload": payload,
            "mode": "purchase",
            "message": "purchase_review",
            "reply": reply,
            "progressed": progressed,
            "pending": None,
        }

    def _terms_words(self, product, amount, terms):
        """Display words for every signed preference of a purchase review (C1)."""
        index = 0 if get_locale() == "zh-CN" else 1
        terms = terms or {}
        separator = "、" if index == 0 else ", "
        account = terms.get("source_account_ref", "A_CURRENT")
        types = terms.get("allowed_types") or ["wealth"]
        objective = terms.get("objective", getattr(self.controller, "_objective", "net_return"))
        return {
            "product": self._shown_name(self._vocabulary()["products"], product),
            "amount": _yuan(amount),
            "account": _ACCOUNT_WORDS.get(account, (account, account))[index],
            "horizon": terms.get("horizon_days", "—"),
            "access": terms.get("max_access_days", "—"),
            "types": separator.join(_TYPE_WORDS.get(item, (item, item))[index] for item in types),
            "objective": _OBJECTIVE_WORDS.get(objective, (objective, objective))[index],
        }

    def _wealth_hold(self, text, service):
        """C1: a purchase or redemption request that orders nothing now, answered in chat."""
        kind = mood(text)
        if kind is not None:
            return {
                "payload": {"status": "clarify", "mood_hold": {"kind": kind, "service": service}},
                "mode": "none",
                "message": "mood_hold",
                "reply": None,
                "progressed": False,
                "pending": None,
            }
        if names_a_time(text):
            return {
                "payload": {"status": "clarify", "time_hold": {"service": service}},
                "mode": "none",
                "message": "time_hold_wealth",
                "reply": None,
                "progressed": False,
                "pending": None,
            }
        return None

    def _redeem(self, text, request_id):
        """One of the customer's own active holdings, through the redemption preparer."""
        held_mood = self._wealth_hold(text, "redeem")
        if held_mood is not None:
            # C1: a question, supposition, past statement or time word redeems nothing.
            return held_mood["payload"], "none", held_mood["message"], None
        state = self.controller.state
        positions = [
            item
            for item in sorted(state.positions.values(), key=lambda row: row.id)
            if item.owner == self.controller.device.principal
            and item.status == "active"
            and item.units - item.reserved_units > 0
        ]
        if not positions:
            return (
                {"status": "clarify", "redemption_parse": {"status": "none"}},
                "none",
                "redeem_none",
                None,
            )
        vocabulary = self._vocabulary()
        names = {ref: list(entry.values()) for ref, entry in vocabulary["products"].items()}
        products = [
            row
            for row in self._product_rows()
            if row["ref"] in {item.product_ref for item in positions}
        ]
        resolution = product_candidates(text, products, names)
        held = list(positions)
        if resolution["status"] == "resolved":
            positions = [item for item in positions if item.product_ref == resolution["ref"]]
        elif resolution["status"] == "no_match":
            # Review fix: a named product the customer does not hold never falls back to a
            # holding of another product; the holdings are listed instead.
            positions = []
        elif resolution["clue"] == "partial":
            positions = [item for item in positions if item.product_ref in resolution["refs"]]
        if len(positions) != 1:
            shown = positions or held
            zh = get_locale() == "zh-CN"
            items = ("；" if zh else "; ").join(
                f"{self._shown_name(vocabulary['products'], item.product_ref)}"
                + (
                    f"（{_yuan(item.units - item.reserved_units)} 份）"
                    if zh
                    else f" ({_yuan(item.units - item.reserved_units)} units)"
                )
                for item in shown
            )
            status, key = (
                ("unresolved", "redeem_choose") if positions else ("no_match", "redeem_no_match")
            )
            return (
                {
                    "status": "awaiting_device_selection",
                    "redemption_parse": {
                        "status": status,
                        "candidates": [item.id for item in shown],
                    },
                },
                "none",
                key,
                _message(key).format(items=items),
            )
        position = positions[0]
        available = position.units - position.reserved_units
        # C1 (review of B1): only 全部 / 全额 redeems all of it, and only one exact amount
        # the holding covers redeems part of it. A share (一半, 10%), units (5000份), "keep
        # X", another number or no quantity at all is asked: the one holding is shown with
        # what can be redeemed. A unique holding never stands in for the quantity.
        quantity = redemption_quantity(self._quantity_text(text))
        over = quantity["mode"] == "amount" and Fraction(quantity["amount"]) > available
        if quantity["mode"] not in {"all", "amount"} or over:
            zh = get_locale() == "zh-CN"
            item = self._shown_name(vocabulary["products"], position.product_ref) + (
                f"（可赎回 {_yuan(available)} 份）" if zh else f" ({_yuan(available)} units)"
            )
            key = "redeem_quantity_over" if over else "redeem_quantity"
            return (
                {
                    "status": "clarify",
                    "redemption_parse": {
                        "status": "quantity",
                        "reason": "over" if over else quantity["mode"],
                        "position_ref": position.id,
                        "candidates": [position.id],
                    },
                },
                "none",
                key,
                _message(key).format(items=item),
            )
        amount = quantity["amount"] if quantity["mode"] == "amount" else fraction_string(available)
        payload, mode, message = self._prepared(
            lambda: self.controller.redeem(position.id, amount, request_id=request_id),
            {"kind": "redeem_product", "amount": amount, "position_ref": position.id},
            {"redemption_parse": {"status": "complete", "position_ref": position.id}},
            request_id,
        )
        return payload, mode, message, None

    def aa_roster(self):
        """Verified contacts the customer owns: the only selectable split-bill participants."""
        return sorted(
            trusted_slot_context(self.controller.state, self.controller.device.principal)[
                "contact_labels"
            ]
        )

    def prepare_aa(self, response_id, participants, *, request_id=None):
        """Complete a split bill by choosing exactly people - 1 trusted contacts in chat.

        The source response is never rewritten. The split-bill form's own preparer builds a
        new unsigned card under a new response, and only the device gesture signs it.
        """
        if response_id not in self._responses:
            raise ValueError("Unknown response identity")
        source = self._responses[response_id]
        payload = source["payload"]
        split = payload.get("aa")
        parse = payload.get("aa_parse") or {}
        if (
            source["intent"] != "transfer"
            or source["status"] != "awaiting_device_selection"
            or not isinstance(split, dict)
            or parse.get("status") != "unresolved"
            or parse.get("reason") not in {"participants", "ambiguous"}
        ):
            raise ValueError("This response has no split bill awaiting participants")
        refs = list(participants)
        if (
            len(refs) != split["participants"] - 1
            or len(set(refs)) != len(refs)
            or any(ref not in self.aa_roster() for ref in refs)
        ):
            raise ValueError("Choose exactly the remaining participants from verified contacts")
        key = (response_id, tuple(refs))
        if key in self._aa_choices:
            return deepcopy(self._responses[self._aa_choices[key]])
        collect = fraction_string(Fraction(split["per_person"]) * len(refs))
        self._sequence += 1
        identifier = stable_id(self.task_id, self._sequence, "conversation-response")
        request_id = request_id or identifier
        completed = {
            "status": "complete",
            "participants": refs,
            "people": split["participants"],
            "total": payload["parameters"]["amount"],
            "per_person": split["per_person"],
            "collect_total": collect,
            "source_response": response_id,
        }
        result, mode, message = self._prepared(
            lambda: self.controller.preview_aa_collection(collect, refs, request_id=request_id),
            {"kind": "aa_request", "participant_refs": refs, "amount": collect},
            {
                "parameters": deepcopy(payload["parameters"]),
                "aa": deepcopy(split),
                "aa_parse": completed,
            },
            request_id,
        )
        response = self._envelope(
            identifier,
            IntentCandidate(intent="transfer", needs_clarification=False),
            result,
            mode=mode,
            message=message,
        )
        self._aa_choices[key] = identifier
        origin = next(row for row in self.history if row["response_id"] == response_id)
        self.history.append({
            "utterance_hash": origin["utterance_hash"],
            "response_id": identifier,
            "intent": response["intent"],
            "status": response["status"],
        })
        # SG-2: a chosen reply's card may carry the S6 warning too.
        domestic_suggest.observe(self, None, response)
        return response

    def _control_payload(self, bank, wealth):
        execution = wealth.get("execution")
        status = (
            execution["status"]
            if bank["status"] == "no_active_plan" and execution
            else bank["status"]
        )
        return {
            **bank,
            "status": status,
            "bank": deepcopy(bank),
            "wealth": deepcopy(wealth),
            "rule_ids": sorted(_recorded_rules(bank) | _recorded_rules(wealth)),
            "audit_checkpoints": {
                "bank": self.controller.gateway.audit.checkpoint(),
                "wealth": self.wealth_controller.gateway.audit.checkpoint(),
            },
        }

    def withdraw_pending(self):
        """Retire channel buttons after an explicit Stop in any shared UI channel."""
        for response in self._responses.values():
            # An established scheduled authorization is terminal for its chat response.
            if response["status"] not in {
                "settled",
                "refused",
                "withdrawn",
                "rejected",
                "controlled",
            }:
                response.update(status="withdrawn", buttons=[], message=_message("withdrawn"))

    def _retired(self, identifier):
        response = self._responses[identifier]
        mode, payload = self._modes[identifier], response["payload"]
        stores = {
            "transfer": (self.controller._drafts, "draft_id"),
            "pending": (self.controller._pending, "pending_id"),
            "plan": (self.controller._plans, "plan_id"),
        }
        if mode in stores:
            objects, field = stores[mode]
            if payload.get(field) not in objects:
                response.update(status="withdrawn", buttons=[], message=_message("withdrawn"))
                return True
        return False

    def _update(self, identifier, payload):
        response = self._responses[identifier]
        response["payload"] = deepcopy(payload)
        response["status"] = payload["status"]
        response["rule_ids"] = sorted(set(response["rule_ids"]) | set(payload.get("rule_ids", [])))
        response["audit_hash"] = self.controller.gateway.audit.checkpoint()["head"]
        # C2: a gesture the recipient check refused says where to answer it (the IM text
        # channel shows this message as it is).
        response["message"] = _message("scam_gate" if payload["status"] == "paused" else "result")
        if payload.get("card"):
            response["card"] = deepcopy(payload["card"])
            response["tier"] = payload["card"].get("tier")
        if payload["status"] == "waiting_mfa":
            response["buttons"] = [
                {"id": "verify_mfa", "response_id": identifier, "requires_device": True}
            ]
        elif payload["status"] == "mfa_verified":
            response["buttons"] = [
                {"id": "confirm", "response_id": identifier, "requires_device": True}
            ]
        elif payload["status"] in {
            "settled",
            "rejected",
            "refused",
            "withdrawn",
            "outside_lock_month",
            "controlled",
        }:
            response["buttons"] = []
        return deepcopy(response)

    def confirm(self, response_id: str, *, selected_node_ids: list[str] | None = None):
        if response_id not in self._responses:
            raise ValueError("Unknown response identity")
        response = self._responses[response_id]
        if response["status"] in {"settled", "withdrawn", "refused", "rejected", "controlled"}:
            return deepcopy(response)
        if self._retired(response_id):
            return deepcopy(response)
        mode, payload = self._modes[response_id], response["payload"]
        if mode == "transfer":
            result = self._call(self.controller.confirm_transfer, payload["draft_id"])
            if result.get("pending_id"):
                self._modes[response_id] = "pending"
        elif mode == "pending":
            result = self._call(self.controller.approve_pending, payload["pending_id"])
            if "pending_id" not in result:
                result["pending_id"] = payload["pending_id"]
        elif mode == "purchase":
            # B1: this device gesture signs the reviewed preference through the wealth page's
            # own preparer; the red card then needs MFA and a final approval.
            purchase = payload["purchase"]
            reviewed = payload.get("purchase_terms")
            current = self.purchase_review_terms(
                purchase["product_ref"], purchase["amount"], purchase["objective"]
            )
            if reviewed is None or current is None or current != reviewed:
                # C1 (R-10): terms that changed after review, or that cannot be read back,
                # are never signed; the user reviews the new terms first (R2-E4).
                response.update(
                    status="clarify",
                    buttons=[],
                    message=_message("purchase_changed"),
                    payload={**deepcopy(payload), "status": "clarify", "terms_changed": True},
                )
                self._modes[response_id] = "none"
                return deepcopy(response)
            result = self._call(
                self.controller.preview_investment,
                purchase["product_ref"],
                amount=purchase["amount"],
                objective=purchase["objective"],
                request_id=f"{response_id}:purchase",
            )
            if result.get("pending_id"):
                self._modes[response_id] = "pending"
                signed = self._signed_terms(result["pending_id"])
                if signed is None or signed != reviewed:
                    # Belt and braces: a signed preference that differs from the review, or
                    # one that cannot be read back, is withdrawn before any MFA or dispatch
                    # (R2-E4). The device already signed that input: it is kept and marked
                    # as withdrawn, never deleted as if it had not been signed.
                    withdrawn = self._withdraw_signed_input(
                        result["pending_id"], "unreadable" if signed is None else "differs"
                    )
                    response.update(
                        status="clarify",
                        buttons=[],
                        message=_message("purchase_changed"),
                        payload={
                            **deepcopy(payload),
                            "status": "clarify",
                            "terms_changed": True,
                            "withdrawn_input": withdrawn,
                        },
                    )
                    self._modes[response_id] = "none"
                    return deepcopy(response)
        elif mode == "plan":
            result = self._call(
                self.controller.confirm_plan, payload["plan_id"], selected_node_ids or []
            )
            if result.get("status") == "selection_required":
                return {
                    **deepcopy(response),
                    "status": "selection_required",
                    "rule_ids": result.get("rule_ids", []),
                }
        else:
            return deepcopy(response)
        return self._update(response_id, result)

    def _withdraw_signed_input(self, pending_id, reason):
        """Withdraw a purchase pending whose signed preference failed the comparison, and
        record the orphaned signed input (it stays in the trusted store, unreferenced by
        any action that can still be approved)."""
        pending = self.controller._pending.pop(pending_id, None) or {}
        prepared = pending.get("prepared")
        args = getattr(getattr(prepared, "action", None), "args", None)
        reference = getattr(args, "preference_ref", None)
        record = {"pending_id": pending_id, "preference_ref": reference, "reason": reason}
        self.withdrawn_inputs.append(record)
        return deepcopy(record)

    def _signed_terms(self, pending_id):
        """The preference a purchase pending actually signed, without its id (C1)."""
        pending = self.controller._pending.get(pending_id) or {}
        prepared = pending.get("prepared")
        if prepared is None:
            return None
        reference = getattr(prepared.action.args, "preference_ref", None)
        item = self.controller.state.user_inputs.get(reference)
        if item is None or not isinstance(item.value, dict):
            return None
        value = dict(item.value)
        value.pop("id", None)
        return value

    def verify_mfa(self, response_id: str):
        if response_id not in self._responses:
            raise ValueError("Unknown response identity")
        response = self._responses[response_id]
        pending = response["payload"].get("pending_id")
        if response["status"] in {"settled", "withdrawn", "refused", "rejected", "controlled"}:
            return deepcopy(response)
        if self._retired(response_id):
            return deepcopy(response)
        if not pending:
            return {**deepcopy(response), "status": "intent_confirmation_required"}
        result = self._call(self.controller.request_mfa, pending)
        if result["status"] != "waiting_mfa":
            return {**deepcopy(response), "status": result["status"]}
        result = self._call(self.controller.complete_mfa, pending)
        result["pending_id"] = pending
        return self._update(response_id, result)

    def response(self, response_id):
        return deepcopy(self._responses[response_id])
