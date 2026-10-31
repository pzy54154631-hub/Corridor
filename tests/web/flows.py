"""Equivalence flows (design §3.5.1): one gesture list, run by two drivers.

Each step is one user gesture followed by one state observation under the step's stage
name. Sentences come from the HEAD test constants, so a later product change to them
reaches these flows too. ``tier`` follows design §1.1: ``core`` gates the main-demo
switch, ``ext`` items are checked once their feature ships, ``after_b`` waits for the
product revision batch B.
"""

from dataclasses import dataclass

from tests.unit.test_ui_scam_friction import (
    CLARIFY,
    CLARIFY_PLAIN,
    FRAUD,
    GUIDED,
    KNOWN_AUTHORITY,
    NEW,
    SAFE,
)
from tests.unit.test_ui_scam_r7 import DIRECTED_NEW, HELD, OPENING

YELLOW = "给妈妈转500元"
RED = "给妈妈转5000元"
URGENT = "马上给妈妈转500元"
BALANCE = "查询余额"
TO_OPERATOR = "转人工"
CARD_LOST = "我的卡丢了"
BIRTHDAY = "帮我准备老婆的生日"
STOP = "停止"
REFUND = "退回锁定的预算"
BILL = "看看9月账单"
ANOMALY = "有没有异常扣款"
SUBSCRIPTIONS = "查看我的订阅"
SUBSCRIPTION_CANCEL = "取消订阅"
SCHEDULED = "每月5号给房东转500元"

OPS = {
    "send",
    "shortcut",
    "answer",
    "retry",
    "select",
    "subscription_choice",
    "confirm",
    "mfa_request",
    "mfa_verify",
    "mfa_wrong",
    "cancel",
    "plan",
    "handoff",
    "compensate",
    "confirm_compensation",
    "wealth_compare",
    "wealth_purchase",
    "preset_r4",
    "subs_query",
    "subs_cancel",
    "stop_all",
    "locale",
}


@dataclass(frozen=True)
class Step:
    op: str
    stage: str
    arg: object = None


@dataclass(frozen=True)
class Flow:
    name: str
    tier: str
    steps: tuple[Step, ...]
    covers: str = ""
    features: frozenset[str] = frozenset()
    left: str = "streamlit"


def send(text, stage="card"):
    return Step("send", stage, text)


def shortcut(key, stage="reply"):
    return Step("shortcut", stage, key)


def answer(answers, stage="answered"):
    return Step("answer", stage, dict(answers))


def retry(stage="retried"):
    return Step("retry", stage)


def select(contact, stage="selected"):
    return Step("select", stage, contact)


def confirm(stage="confirmed"):
    return Step("confirm", stage)


def cancel(stage="cancelled"):
    return Step("cancel", stage)


def plan(nodes, stage):
    """Tick plan nodes by display index (or ``"all"``) and confirm when the device offers
    it: Streamlit ticks the boxes and clicks Confirm; the web registers the ticks and
    confirms only with a gesture token (P-DIFF-3: an invalid selection gets none)."""
    return Step("plan", stage, nodes if nodes == "all" else tuple(nodes))


def wealth(stage="purchase_card", choice=None):
    """The wealth tab's purchase card (``None``: the highest-ranked eligible product)."""
    return Step("wealth_purchase", stage, choice)


def handoff(stage="handoff"):
    """The task panel's handoff button (the web's ``/api/chat/handoff``)."""
    return Step("handoff", stage)


def compensate(stage="compensation"):
    """The task panel's Compensate (the web's ``/api/chat/compensate``) and its card."""
    return (Step("compensate", stage), Step("confirm_compensation", "compensated"))


def stop_all(stage="stopped"):
    """The top bar's Stop all (the web's ``/api/stop``)."""
    return Step("stop_all", stage)


def locale(value, stage=None):
    return Step("locale", stage or f"locale_{value}", value)


def mfa_wrong(stage):
    return Step("mfa_wrong", stage)


def red_path():
    """Confirm, request the SMS code, type it, confirm again (INV-2.6 order)."""
    return (
        confirm(),
        Step("mfa_request", "challenge"),
        Step("mfa_verify", "verified"),
        confirm("final"),
    )


def _flow(name, tier, covers, *steps, features=(), left="streamlit"):
    flat = []
    for step in steps:
        flat.extend(step if isinstance(step, tuple) else (step,))
    stages = [step.stage for step in flat]
    assert len(stages) == len(set(stages)), (name, stages)
    assert all(step.op in OPS for step in flat), name
    return Flow(name, tier, tuple(flat), covers, frozenset(features), left)


FLOWS = {
    flow.name: flow
    for flow in (
        _flow("level0_yellow_known", "core", "P0-2 M0 probe", send(YELLOW), confirm()),
        _flow("level0_large_known", "core", "P0-2 red M0 probe", send(RED), red_path()),
        _flow("level0_urgency_only", "core", "P0-3 no gate", send(URGENT), confirm()),
        _flow(
            "level0_then_query",
            "core",
            "green after yellow",
            send(YELLOW),
            confirm(),
            send(BALANCE, "query"),
        ),
        _flow("level1_new_no_cue", "core", "P0-3", send(NEW), answer(SAFE), red_path()),
        _flow(
            "level1_known_authority",
            "core",
            "P0-3",
            send(KNOWN_AUTHORITY),
            answer(SAFE),
            confirm(),
        ),
        _flow("level2_fraud_new", "core", "P0-3", send(FRAUD), answer(SAFE), red_path()),
        _flow(
            "level2_pause_retry",
            "core",
            "P0-3 M0 probe",
            send(FRAUD),
            answer(GUIDED, "paused"),
            retry(),
            answer(SAFE),
            red_path(),
        ),
        _flow(
            "directed_preset",
            "core",
            "P0-3 R7 preset",
            send(DIRECTED_NEW),
            retry(),
            answer(SAFE),
            red_path(),
        ),
        _flow("opening_copy", "core", "P0-3 R7 opening", send(OPENING), answer(SAFE), red_path()),
        _flow("phrase_gate_held", "core", "P0-4", send(HELD, "held")),
        _flow("drill_phrase_gate", "core", "P0-4", shortcut("scam_drill", "held")),
        # The chosen Lin is a known contact and 200 stays under the yellow limit, so the
        # card is yellow (level 1 for the authority words); there is no SMS step.
        _flow(
            "clarify_lin_b",
            "core",
            "P0-5",
            send(CLARIFY, "clarify"),
            select("CONTACT_LIN_B"),
            answer(SAFE),
            confirm(),
        ),
        _flow(
            "clarify_cancel",
            "core",
            "P0-5 O-S13",
            send(CLARIFY_PLAIN, "clarify"),
            select("CONTACT_LIN_A"),
            cancel(),
        ),
        _flow("cancel_mid_card", "core", "P0-7", send(YELLOW), cancel()),
        _flow("handoff_chat", "core", "P0-7", send(YELLOW), send(TO_OPERATOR, "handoff")),
        _flow("handoff_panel", "core", "P0-7 task panel", send(YELLOW), handoff()),
        _flow("chat_stop", "core", "P0-7 typed stop", send(YELLOW), send(STOP, "stopped")),
        _flow("stop_all_mid_card", "core", "P0-7 top bar", send(RED), confirm(), stop_all()),
        _flow(
            "mfa_lockout",
            "core",
            "P0-15",
            send(RED),
            confirm(),
            Step("mfa_request", "challenge"),
            mfa_wrong("wrong_1"),
            mfa_wrong("wrong_2"),
            mfa_wrong("wrong_3"),
        ),
        _flow(
            "locale_switch_gate",
            "core",
            "P0-14 O-M2",
            send(FRAUD),
            answer(SAFE),
            locale("en"),
            red_path(),
        ),
        # Switched there and back before any gesture: the gate record and tokens survive
        # both switches and every text is written in Chinese on both pages (Q-1 option B).
        _flow(
            "locale_round_trip",
            "core",
            "P0-14 O-M2",
            send(FRAUD),
            answer(SAFE),
            locale("en"),
            locale("zh-CN", "locale_zh"),
            red_path(),
        ),
        _flow("card_lost", "core", "generic rendering", send(CARD_LOST), red_path()),
        # ext 1. Ticking n1 alone is not a prefix: Streamlit's Confirm gets
        # selection_required, the web gives no token. A true prefix re-previews a plan of
        # the ticked nodes without signing; ticking all of that plan signs it.
        _flow(
            "birthday_prefix",
            "ext",
            "P0-11",
            send(BIRTHDAY, "plan"),
            plan([1], "invalid"),
            plan([0], "subset"),
            plan("all", "confirmed"),
            features=("plan_select",),
        ),
        # The same without the invalid ticking (CONTRACT-CHANGES.md Q-2): Streamlit's
        # refused Confirm still counts one session call, the web makes none (P-DIFF-3).
        _flow(
            "birthday_subset",
            "ext",
            "P0-11",
            send(BIRTHDAY, "plan"),
            plan([0], "subset"),
            plan("all", "confirmed"),
            features=("plan_select",),
        ),
        _flow(
            "birthday_panel_compensate",
            "ext",
            "P0-7 P0-11 task panel",
            send(BIRTHDAY, "plan"),
            plan("all", "confirmed"),
            send(STOP, "stopped"),
            compensate(),
            features=("plan_select", "compensate"),
        ),
        _flow(
            "birthday_then_refund",
            "ext",
            "P0-7 P0-11",
            send(BIRTHDAY, "plan"),
            plan("all", "confirmed"),
            send(STOP, "stopped"),
            send(REFUND, "refund"),
            confirm("refunded"),
            features=("plan_select",),
        ),
        # ext 3 and ext 2 over chat: generic rendering of the replies' cards and receipts,
        # reachable by typing in any build; the accounts tab adds only a projection.
        _flow(
            "subscriptions_chat",
            "ext",
            "P0-10",
            send(SUBSCRIPTIONS, "query"),
            confirm("listed"),
            send(SUBSCRIPTION_CANCEL, "choose"),
            Step("subscription_choice", "cancel_card", "c0"),
            confirm("cancelled"),
        ),
        _flow("bill_report", "ext", "P0-9", send(BILL, "bill"), send(ANOMALY, "anomaly")),
        # ext 4 on the wealth tab and the device: compared with the reference script.
        _flow(
            "wealth_purchase",
            "ext",
            "P0-8",
            Step("wealth_compare", "compared", "net_return"),
            wealth(),
            Step("mfa_request", "challenge"),
            Step("mfa_verify", "verified"),
            confirm("final"),
            features=("tab:wealth",),
            left="reference",
        ),
        _flow(
            "stop_then_purchase",
            "ext",
            "P0-8 O-S12",
            Step("wealth_compare", "compared", "net_return"),
            wealth(),
            stop_all(),
            wealth("purchase_again"),
            Step("mfa_request", "challenge"),
            Step("mfa_verify", "verified"),
            confirm("final"),
            features=("tab:wealth",),
            left="reference",
        ),
        # The R4 refusal preset runs on its own controller: the session is untouched.
        _flow(
            "r4_preset",
            "ext",
            "P0-8 INV-11.5",
            Step("preset_r4", "preset"),
            features=("tab:wealth",),
            left="reference",
        ),
        # ext 3 fallback on the accounts tab: query, confirm, cancel the first, confirm.
        _flow(
            "subscriptions_tab",
            "ext",
            "P0-10 accounts tab",
            Step("subs_query", "query_card"),
            confirm("listed"),
            Step("subs_cancel", "cancel_card", "c0"),
            confirm("cancelled"),
            features=("tab:accounts", "shortcut:subscriptions"),
            left="reference",
        ),
        # Waits for the product batch B (date parsing); run now to show where it stands.
        _flow("scheduled_monthly", "after_b", "batch B", send(SCHEDULED), red_path()),
    )
}

PROBES = ("level0_yellow_known", "level0_large_known", "level2_pause_retry")


def selected(names=None, *, tiers=("core",)):
    """Flows by name, or every flow of the given tiers in table order."""
    if names:
        return [FLOWS[name] for name in names]
    return [flow for flow in FLOWS.values() if flow.tier in tiers]
