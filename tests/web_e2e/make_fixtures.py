"""Generate and check ``tests/web_e2e/fixtures/*.json`` from the contract's appendix (11).

    python tests/web_e2e/make_fixtures.py           # write, then check
    python tests/web_e2e/make_fixtures.py --check   # check only (exit 1 on any problem)

Each fixture is one complete ``Ok<{state}>`` or ``Err`` response (contract 1.3), in both
locales (``*-en.json``). The check walks every value against the contract's types (4, 5),
with judge-only keys required in the judge view and absent in the customer view; when
``corridor.web.contracts`` exposes a validator it is run as well. Texts are illustrative:
the server catalog is authoritative.
"""

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / "fixtures"

# --- texts ---------------------------------------------------------------------------------

TEXT = {
    "zh-CN": {
        "corner": "合成数据 · 模拟认证 · 无真实资金",
        "badge": "脚本演示",
        "clock": "2026-09-15 10:30（北京时间）",
        "bank": "演示银行",
        "assistant": "智能助手",
        "judge_strip": "评委视图：显示内核裁决、哈希与审计证据；视图只是展示偏好，不是访问控制",
        "sc_transfer": "给妈妈转 800 元",
        "sc_cards": "我的卡丢了",
        "sc_drill": "诈骗演练",
        "sc_birthday": "帮我准备老婆的生日",
        "drill_caption": "演练：模拟一段“工作人员让您立即转账”的话术，看系统如何回应",
        "drill_judge": "（评委）演练句命中话术闸门：不出卡，派发 0",
        "placeholder": "说说您要办理什么，例如：给妈妈转500元",
        "attach": "附上对方发来的文件（模拟）",
        "attach_note": "附件开关由您自报，系统不读取任何文件",
        "contacts_note": "演示联系人：妈妈、房东、同学、邻居、新联系人、爱人",
        "cancel": "取消",
        "cancel_hint": "取消会撤回本会话所有待确认提案；已结算的不变",
        "red": "红 · 需独立模拟验证",
        "yellow": "黄 · 需设备确认",
        "green": "绿 · 自动查询",
        "black": "黑 · 禁止执行",
        "card_title": "网关审批卡",
        "purpose": "转账",
        "payee": "收款人",
        "fee": "手续费",
        "fee_value": "¥0.00（无手续费）",
        "mom": "妈妈 · 尾号3721（合成）",
        "new_payee": "新联系人 · 尾号5512（合成）",
        "not_signed": "不属于签名内容",
        "why": "原因：金额超过红档阈值，需要独立模拟验证",
        "tier_reason_red": "单笔超过 2,000 元",
        "tier_reason_yellow": "向新收款人转账",
        "red_note": "红档操作：设备确认后还需输入模拟短信验证码",
        "waiting_mfa": "等待验证",
        "copy_red": "完成模拟验证后再确认此操作。",
        "device_title": "设备确认（模拟）",
        "device_title_judge": "模拟签名设备（软件模拟）",
        "idle": "当前没有需要在设备上确认的操作",
        "source_chat": "对话",
        "step_mfa_code": "请输入模拟手机收到的 6 位验证码",
        "sent": "验证码已发送到模拟手机（尾号8866，合成）",
        "method": "短信验证码（模拟）· 其他强验证方式未接入",
        "mfa_hint": "验证码只显示在右侧模拟短信里",
        "submit_code": "提交验证码",
        "sms_label": "模拟短信（合成，不是真实短信）",
        "sms_open": "在新窗口打开",
        "sms_footer": "合成短信 · 无短信网关 · 无真实手机号",
        "user_red": "给妈妈转5000元",
        "user_directed": "对方让我给新联系人转480元",
        "user_held": "反诈中心让我立即给新联系人转480元，不然账户冻结",
        "user_clarify": "给Lin转200元",
        "user_birthday": "帮我准备老婆的生日",
        "awaiting": "等待作答",
        "gate_title": "转账前请先回答两个问题",
        "gate_subtitle": "这两题不属于签名内容，只用来决定是否先暂停",
        "facts_title": "系统登记的事实",
        "gate_fact": "首次向该收款人转账",
        "cues": "您的原话提到有人让您转账",
        "questions_title": "请如实作答",
        "q_verified": "您是否已通过官方渠道独立核实收款人？",
        "q_guided": "是否有人正在电话或消息里指导您转账？",
        "yes": "是",
        "no": "否",
        "unsure": "不确定",
        "pause_title": "已暂停这笔转账",
        "pause_copy": "您的原话提到有人让您转账，第二题已按原话预置为“是”。请先挂断电话，"
        "通过卡背面的官方电话核实。",
        "retry": "重新作答",
        "self_report": "作答由您自述，系统无法核实",
        "pause_cancel": "如不再需要，可直接取消",
        "footer_gate": "两道题只决定是否暂停；签名对象仍是设备显示的这张卡",
        "judge_note": "门控记录：epoch 1，第二题预置",
        "judge_record": "gate epoch=1 answers={guided: yes(preset)}",
        "preset_note": "第二题按原话预置为“是”，只有“重新作答”能清除",
        "step_gate_blocked": "两道题未通过，设备暂不提供确认",
        "gate_notice": "请先在手机上完成两道题",
        "back": "回到那条回复",
        "phrase_gate": "这类说法是常见诈骗话术。公安反诈专线是 96110；如有疑问，请拨打卡背面的"
        "官方电话。本次不会生成转账卡。",
        "phrase_judge": "（评委）话术闸门命中：权威冒充 + 立即转账，未出卡，派发 0",
        "status_phrase": "已拦截",
        "status_clarify": "请选择收款人",
        "clarify_label": "找到多个叫 Lin 的联系人，请选择",
        "lin_a": "Lin · 尾号0187（合成）",
        "lin_b": "Lin · 尾号4420（合成）",
        "review": "核对收款人",
        "locked_text": "这笔已提交核对，如需换人请先取消",
        "plan_rule": "请从第一项起连续勾选；未勾选的项不会执行",
        "plan_nodes": ["订蛋糕 ¥268.00", "订花 ¥199.00", "餐厅定金 ¥500.00", "礼物 ¥1,299.00"],
        "status_plan": "等待勾选",
        "step_subset": "将为所选 2 项生成新的待签计划卡，本步不签名",
        "selection": "已选 2 / 4 项",
        "make_card": "生成新卡",
        "plan_card": "供设备核对的事实 · 尚未授权",
        "plan_purpose": "生日计划",
        "locked": "已锁定：黄色和红色操作会被拒绝，绿色查询仍可使用。",
        "way_out": "解锁需要冷却期、本人设备确认和模拟短信验证；本网页暂未提供，可在兜底界面完成，"
        "或点“重新开始”换一个新的合成会话",
        "unlock_after": "2026-09-15 11:02（北京时间）",
        "step_locked": "账户已锁定，黄红档操作暂不可用",
        "status_failed": "验证失败",
        "copy_locked": "验证码连续错误，账户已锁定。",
        "card_changed": "卡片内容已更新，请重新核对后再确认",
        "stale": "这个页面属于已重置的会话，已按新会话刷新",
        "login_title": "进入合成演示",
        "login_intro": "这是合成数据的演示银行，不连接任何真实账户",
        "login_button": "进入合成演示",
        "login_caption": "模拟认证 · 无真实资金",
        "greeting": "您好，我是智能助手，可以帮您转账、查询和挂失",
        "planner_badge": "脚本规划",
        "planner_source": "脚本规划（离线）",
        "audit_note": "审计校验只说明链未被改写，不证明外部世界发生了什么",
        "intent_note": "意图日志不在哈希链内",
        "evidence_footer": "证据只读；视图只是展示偏好，不是访问控制",
        "kernel_line": "APR-NEWPAYEE：首次收款人需设备确认",
        "not_yet": "尚未产生",
    },
    "en": {
        "corner": "Synthetic data · simulated authentication · no real money",
        "badge": "Scripted demo",
        "clock": "2026-09-15 10:30 (Beijing time)",
        "bank": "Demo Bank",
        "assistant": "Assistant",
        "judge_strip": "Judge view: kernel verdicts, hashes and audit evidence; the view is a "
        "display preference, not access control",
        "sc_transfer": "Send Mom 800 yuan",
        "sc_cards": "I lost my card",
        "sc_drill": "Scam drill",
        "sc_birthday": "Plan my wife's birthday",
        "drill_caption": "Drill: a rehearsed 'staff told me to transfer now' message, to show "
        "how the system answers",
        "drill_judge": "(Judge) The drill sentence hits the phrase gate: no card, 0 dispatches",
        "placeholder": "Tell me what you need, e.g. send Mom 500 yuan",
        "attach": "Attach a file the other party sent (simulated)",
        "attach_note": "The attachment switch is self-reported; no file is read",
        "contacts_note": "Demo contacts: Mom (妈妈), Landlord (房东), Classmate (同学), "
        "Neighbor (邻居), New contact (新联系人), Spouse (爱人)",
        "cancel": "Cancel",
        "cancel_hint": "Cancel withdraws every pending proposal in this session; settled ones stay",
        "red": "Red · independent simulated verification",
        "yellow": "Yellow · device confirmation",
        "green": "Green · automatic query",
        "black": "Black · not allowed",
        "card_title": "Gateway approval card",
        "purpose": "Transfer",
        "payee": "Payee",
        "fee": "Fee",
        "fee_value": "¥0.00 (no fee)",
        "mom": "Mom · ending 3721 (synthetic)",
        "new_payee": "New contact · ending 5512 (synthetic)",
        "not_signed": "Not part of the signed content",
        "why": "Why: the amount is above the red threshold and needs independent verification",
        "tier_reason_red": "Single transfer above 2,000 yuan",
        "tier_reason_yellow": "Transfer to a new payee",
        "red_note": "Red operation: after device confirmation, enter the simulated SMS code",
        "waiting_mfa": "Waiting for verification",
        "copy_red": "Complete the simulated verification, then confirm this operation.",
        "device_title": "Device confirmation (simulated)",
        "device_title_judge": "Simulated signing device (software)",
        "idle": "Nothing needs confirming on the device",
        "source_chat": "Chat",
        "step_mfa_code": "Enter the 6-digit code the simulated phone received",
        "sent": "Code sent to the simulated phone (ending 8866, synthetic)",
        "method": "SMS code (simulated) · other strong methods not connected",
        "mfa_hint": "The code appears only in the simulated SMS panel",
        "submit_code": "Submit code",
        "sms_label": "Simulated SMS (synthetic, not a real text message)",
        "sms_open": "Open in a new window",
        "sms_footer": "Synthetic SMS · no SMS gateway · no real phone number",
        "user_red": "给妈妈转5000元",
        "user_directed": "对方让我给新联系人转480元",
        "user_held": "反诈中心让我立即给新联系人转480元，不然账户冻结",
        "user_clarify": "给Lin转200元",
        "user_birthday": "帮我准备老婆的生日",
        "awaiting": "Waiting for answers",
        "gate_title": "Please answer two questions before this transfer",
        "gate_subtitle": "These questions are not part of the signed content; they only decide "
        "whether to pause first",
        "facts_title": "Facts on record",
        "gate_fact": "First transfer to this payee",
        "cues": "Your message says someone asked you to transfer",
        "questions_title": "Please answer truthfully",
        "q_verified": "Have you independently verified the payee through an official channel?",
        "q_guided": "Is someone guiding you to transfer by phone or message right now?",
        "yes": "Yes",
        "no": "No",
        "unsure": "Not sure",
        "pause_title": "This transfer is paused",
        "pause_copy": "Your message says someone asked you to transfer, so question 2 is preset "
        "to 'Yes'. Hang up first and verify through the official number on your card.",
        "retry": "Answer again",
        "self_report": "Your answers are self-reported; the system cannot verify them",
        "pause_cancel": "If you no longer need it, you can cancel",
        "footer_gate": "The questions only decide whether to pause; the device still signs the "
        "card it shows",
        "judge_note": "Gate record: epoch 1, question 2 preset",
        "judge_record": "gate epoch=1 answers={guided: yes(preset)}",
        "preset_note": "Question 2 preset to 'Yes' from the message; only 'Answer again' clears it",
        "step_gate_blocked": "The two questions did not pass; the device offers no confirmation",
        "gate_notice": "Answer the two questions on the phone first",
        "back": "Back to that reply",
        "phrase_gate": "This is a common scam script. The police anti-fraud line is 96110; if "
        "in doubt, call the official number on the back of your card. No transfer card will "
        "be created.",
        "phrase_judge": "(Judge) Phrase gate hit: impersonated authority + transfer now; no "
        "card, 0 dispatches",
        "status_phrase": "Blocked",
        "status_clarify": "Choose the payee",
        "clarify_label": "Several contacts are called Lin; please choose one",
        "lin_a": "Lin · ending 0187 (synthetic)",
        "lin_b": "Lin · ending 4420 (synthetic)",
        "review": "Review payee",
        "locked_text": "This one is already submitted for review; cancel first to change payee",
        "plan_rule": "Tick from the first item without gaps; unticked items will not run",
        "plan_nodes": [
            "Order cake ¥268.00",
            "Order flowers ¥199.00",
            "Restaurant deposit ¥500.00",
            "Gift ¥1,299.00",
        ],
        "status_plan": "Waiting for selection",
        "step_subset": "A new plan card for the 2 selected items will be prepared; nothing is "
        "signed in this step",
        "selection": "2 of 4 selected",
        "make_card": "Prepare new card",
        "plan_card": "Facts for the device to check · not yet authorised",
        "plan_purpose": "Birthday plan",
        "locked": "Locked: yellow and red operations are refused; green queries still work.",
        "way_out": "Unlocking needs a cool-down, your own device confirmation and a simulated "
        "SMS code; this page does not offer it yet. Use the fallback UI, or press 'Start "
        "over' for a new synthetic session",
        "unlock_after": "2026-09-15 11:02 (Beijing time)",
        "step_locked": "The account is locked; yellow and red operations are unavailable",
        "status_failed": "Verification failed",
        "copy_locked": "Too many wrong codes; the account is locked.",
        "card_changed": "The card has changed; review it again before confirming",
        "stale": "This page belonged to a session that was reset; it now shows the new session",
        "login_title": "Enter the synthetic demo",
        "login_intro": "A demo bank with synthetic data; no real account is connected",
        "login_button": "Enter the synthetic demo",
        "login_caption": "Simulated authentication · no real money",
        "greeting": "Hello, I can help with transfers, queries and reporting a lost card",
        "planner_badge": "Scripted planner",
        "planner_source": "Scripted planner (offline)",
        "audit_note": "Audit verification shows the chain was not rewritten; it does not "
        "prove what happened outside",
        "intent_note": "The intent log is not in the hash chain",
        "evidence_footer": "Evidence is read-only; the view is a display preference, not "
        "access control",
        "kernel_line": "APR-NEWPAYEE: a first-time payee needs device confirmation",
        "not_yet": "Not yet produced",
    },
}

GLYPH = {"green": "circle", "yellow": "diamond", "red": "triangle", "black": "square"}
GENERATION = "q3Lw9xYp0aZk"
NEW_GENERATION = "Jt8vNq2Wm5Yc"
PAIR = "Qm9nUjVxa2Zv_xT1aA"
HASH_A = "3f1c0a9e5b7d2c4e6f8a0b1c2d3e4f5061728394a5b6c7d8e9f0a1b2c3d4e5f6"
HASH_B = "a0b1c2d3e4f5061728394a5b6c7d8e9f3f1c0a9e5b7d2c4e6f8a0b1c2d3e4f5f"


def tier(code: str, t: dict) -> dict:
    return {"code": code, "label": t[code], "glyph": GLYPH[code]}


def hash_vm(full: str) -> dict:
    return {"algorithm": "SHA-256(JCS)", "short": full[:16], "full": full}


def capabilities(**extra) -> dict:
    caps = {
        "tabs": [],
        "shortcuts": ["transfer", "cards", "scam_drill"],
        "live": False,
        "plan_select": False,
        "compensate": False,
        "aa": False,
        "lab": False,
    }
    caps.update(extra)
    return caps


def shortcuts(caps: dict, t: dict) -> list[dict]:
    labels = {
        "transfer": t["sc_transfer"],
        "cards": t["sc_cards"],
        "scam_drill": t["sc_drill"],
        "birthday": t["sc_birthday"],
    }
    return [{"key": key, "label": labels[key]} for key in caps["shortcuts"]]


def phone_url(locale: str, view: str) -> str:
    return f"/phone/{PAIR}?lang={locale}&view={view}"


def card(
    key: str,
    t: dict,
    view: str,
    *,
    code: str,
    amount: str,
    payee: str,
    reason: str,
    notes: list[dict],
    title: str | None = None,
    purpose: str | None = None,
    position: str | None = None,
) -> dict:
    vm = {
        "key": key,
        "title": title or t["card_title"],
        "purpose": purpose or t["purpose"],
        "amount_text": amount,
        "tier": tier(code, t),
        "tier_reason": reason,
        "facts": [
            {"key": "objects.contact_ref", "label": t["payee"], "value": payee},
            {"key": "fees", "label": t["fee"], "value": t["fee_value"]},
        ],
        "notes": notes,
        "not_signed_label": t["not_signed"],
        "why": {"lines": [t["why"]]},
    }
    if position is not None:
        vm["position"] = position
    if view == "judge":
        vm["technical"] = {
            "card": {"kind": "transfer", "amount": amount.lstrip("¥").replace(",", "")},
            "amount_raw": amount.lstrip("¥").replace(",", ""),
            "contact_aliases": [],
        }
        vm["structured"] = [{"label": t["payee"], "lines": [payee]}]
    return vm


def with_key(card_vm: dict, key: str) -> dict:
    return {**card_vm, "key": key}


def response(
    rkey: str,
    t: dict,
    view: str,
    *,
    status: str,
    status_text: str,
    copy: list[str],
    tier_code: str | None,
    cards: list[dict],
    device_task: str | None,
    gate: dict | None = None,
    clarify: dict | None = None,
    plan_select: dict | None = None,
    cancel: bool = True,
    current: bool = True,
    extra_judge: dict | None = None,
) -> dict:
    vm = {
        "rkey": rkey,
        "current": current,
        "status": status,
        "status_text": status_text,
        "copy_lines": copy,
        "tier": tier(tier_code, t) if tier_code else None,
        "requested_tier": None,
        "redisplay_notice": None,
        "cards": cards,
        "gate": gate,
        "result": None,
        "failure": None,
        "live_notes": [],
        "device_task": device_task,
        "route_hint": None,
        "fallback_shortcuts": False,
        "cancel": {"label": t["cancel"], "hint": t["cancel_hint"]} if cancel else None,
    }
    if clarify is not None:
        vm["clarify"] = clarify
    if plan_select is not None:
        vm["plan_select"] = plan_select
    if view == "judge":
        vm["card_source"] = "authoritative"
        vm["technical_url"] = f"/api/evidence/responses/{rkey}"
        vm.update(extra_judge or {})
    return vm


def safety(t: dict, view: str, *, locked: bool = False) -> dict:
    vm = {
        "locked": locked,
        "text": t["locked"] if locked else None,
        "hint": t["way_out"] if locked else None,
    }
    if view == "judge":
        vm.update({
            "state": "Locked" if locked else "Normal",
            "reason": "breaker_mfa" if locked else None,
            "unlock_after_text": t["unlock_after"] if locked else None,
            "rule_ids": ["APR-LOCK", "BRK-LOCK"] if locked else [],
        })
    return vm


def evidence(
    t: dict,
    *,
    rkey: str | None,
    outcome: str | None,
    tier_name: str | None,
    proposal: bool,
    dispatches: int = 0,
    safety_state: str = "Normal",
    reason: str | None = None,
) -> dict:
    vm = {
        "open_by_default": ["kernel", "consistency", "dispatch"],
        "planner": {"badge": t["planner_badge"], "source": t["planner_source"], "counters": []},
        "dispatch": {"count": dispatches, "receipts": []},
        "audit": {"count": 3, "head": hash_vm(HASH_B), "verified": True, "note": t["audit_note"]},
        "safety": {"state": safety_state, "reason": reason},
        "intent_log": [],
        "intent_log_note": t["intent_note"],
        "planner_log": [],
        "footer": t["evidence_footer"],
    }
    if rkey is not None:
        vm["response"] = {
            "rkey": rkey,
            "intent": "transfer",
            "status": "awaiting_input",
            "rule_ids": ["APR-NEWPAYEE"],
            "rule_lines": [t["kernel_line"]],
            "audit_hash": hash_vm(HASH_B),
        }
        vm["kernel"] = {
            "outcome": outcome,
            "tier": tier_name,
            "rule_ids": ["APR-NEWPAYEE"],
            "rule_lines": [t["kernel_line"]],
            "note": None,
        }
        vm["consistency"] = {
            "display_card": hash_vm(HASH_A),
            "proposal_card": hash_vm(HASH_A) if proposal else None,
            "same_hash": True if proposal else None,
            "same_business_facts": True if proposal else None,
            "action": hash_vm(HASH_B) if proposal else None,
            "cert": hash_vm(HASH_B) if proposal else None,
            "card_core": hash_vm(HASH_A) if proposal else None,
            "challenge": "none",
            "not_yet_text": None if proposal else t["not_yet"],
            "mismatch_text": None,
        }
        vm["intent_log"] = [
            {"intent": "transfer", "status": "awaiting_input", "response": rkey, "utterance": ""}
        ]
        vm["planner_log"] = [
            {
                "rkey": rkey,
                "planner": "scripted",
                "intent": "transfer",
                "outcome": outcome,
                "tier": tier_name,
                "rule_ids": ["APR-NEWPAYEE"],
            }
        ]
    return vm


def device(t: dict, view: str, locale: str, tasks: list[dict]) -> dict:
    return {
        "title": t["device_title_judge"] if view == "judge" else t["device_title"],
        "idle_text": t["idle"],
        "queue_text": None,
        "tasks": tasks,
        "active": tasks[0]["key"] if tasks else None,
        "sms": {
            "url": phone_url(locale, view),
            "label": t["sms_label"],
            "open_label": t["sms_open"],
            "footer": t["sms_footer"],
        },
    }


def task(
    key: str,
    rkey: str,
    t: dict,
    view: str,
    *,
    cards: list[dict],
    step: str,
    step_text: str,
    gesture: dict | None,
    **extra,
) -> dict:
    vm = {
        "key": key,
        "source": "chat",
        "source_label": t["source_chat"],
        "rkey": rkey,
        "current": True,
        "cards": cards,
        "step": step,
        "step_text": step_text,
        "gesture": gesture,
        "cancel": {"label": t["cancel"], "hint": t["cancel_hint"]},
    }
    vm.update(extra)
    if view == "judge":
        status = "not_yet" if step in ("gate_blocked", "select_nodes") else "pending"
        vm["hash"] = {**hash_vm(HASH_A), "status": status, "label": "H(display.card)"}
    return vm


def app_state(
    t: dict,
    view: str,
    locale: str,
    *,
    revision: int,
    caps: dict,
    turns: list[dict],
    responses: dict,
    current: str | None,
    tasks: list[dict],
    locked: bool = False,
    generation: str = GENERATION,
    logged_in: bool = True,
    evidence_vm: dict | None = None,
    drill: bool = True,
) -> dict:
    drill_caption = None
    if drill and "scam_drill" in caps["shortcuts"]:
        drill_caption = t["drill_caption"]
        if view == "judge":
            drill_caption += " " + t["drill_judge"]
    shell = {
        "corner": t["corner"],
        "badge": {"kind": "demo", "text": t["badge"]},
        "safety": safety(t, view, locked=locked),
        "paused_banner": None,
        "notices": [],
    }
    if view == "judge":
        shell = {"corner": shell["corner"], "judge_strip": t["judge_strip"], **shell}
    state = {
        "schema": 1,
        "contract": "web-contract/1.1",
        "generation": generation,
        "revision": revision,
        "view": view,
        "locale": locale,
        "capabilities": caps,
        "session": {
            "logged_in": logged_in,
            "clock_text": t["clock"],
            "bank_name": t["bank"],
            "assistant_name": t["assistant"],
            "phone_url": phone_url(locale, view),
        },
        "shell": shell,
    }
    if not logged_in:
        state["login"] = {
            "title": t["login_title"],
            "intro": t["login_intro"],
            "button": t["login_button"],
            "caption": t["login_caption"],
        }
    state["chat"] = {
        "shortcuts": shortcuts(caps, t),
        "drill_caption": drill_caption,
        "greeting": None if turns else t["greeting"],
        "turns": turns,
        "responses": responses,
        "current": current,
        "gated_elsewhere": [],
        "task_panel": None,
        "composer": {
            "placeholder": t["placeholder"],
            "max_chars": 4000,
            "attach_label": t["attach"],
            "attach_note": t["attach_note"],
            "contacts_note": t["contacts_note"],
            "disabled": not logged_in,
        },
    }
    state["device"] = device(t, view, locale, tasks)
    if view == "judge":
        state["evidence"] = evidence_vm or evidence(
            t, rkey=None, outcome=None, tier_name=None, proposal=False
        )
    return state


def ok(state: dict, **extra) -> dict:
    return {
        "ok": True,
        "generation": state["generation"],
        "revision": state["revision"],
        **extra,
        "state": state,
    }


def err(code: str, message: str, state: dict) -> dict:
    return {
        "ok": False,
        "generation": state["generation"],
        "revision": state["revision"],
        "error": {"code": code, "message": message, "retry": False},
        "state": state,
    }


def turns_for(rkey: str, user: str, assistant: str) -> list[dict]:
    index = rkey[1:]
    return [
        {"id": f"u{index}", "role": "user", "rkey": rkey, "text": user},
        {"id": f"a{index}", "role": "assistant", "rkey": rkey, "text": assistant},
    ]


# --- scenarios -----------------------------------------------------------------------------


def red_code(locale: str, view: str = "customer", revision: int = 7) -> dict:
    t = TEXT[locale]
    red_card = card(
        "R2-0",
        t,
        view,
        code="red",
        amount="¥5,000.00",
        payee=t["mom"],
        reason=t["tier_reason_red"],
        notes=[{"kind": "red", "title": None, "lines": [t["red_note"]], "footer": None}],
    )
    gesture = {
        "op": "mfa_verify",
        "token": "k3JxR0h1Z2xvYmFsX3Rva2",
        "card_version": "9f0c6a1d2b3e4f509f0c6a1d2b3e4f50",
        "label": t["submit_code"],
    }
    tasks = [
        task(
            "TR2",
            "R2",
            t,
            view,
            cards=[with_key(red_card, "TR2-0")],
            step="mfa_code",
            step_text=t["step_mfa_code"],
            gesture=gesture,
            mfa={
                "sent_text": t["sent"],
                "method_text": t["method"],
                "hint": t["mfa_hint"],
                "expired_text": None,
            },
        )
    ]
    responses = {
        "R2": response(
            "R2",
            t,
            view,
            status="waiting_mfa",
            status_text=t["waiting_mfa"],
            copy=[t["copy_red"]],
            tier_code="red",
            cards=[red_card],
            device_task="TR2",
        )
    }
    return app_state(
        t,
        view,
        locale,
        revision=revision,
        caps=capabilities(),
        turns=turns_for("R2", t["user_red"], t["copy_red"]),
        responses=responses,
        current="R2",
        tasks=tasks,
        evidence_vm=evidence(t, rkey="R2", outcome="Escalate", tier_name="red", proposal=True)
        if view == "judge"
        else None,
    )


def gate_paused_preset(locale: str, view: str = "judge") -> dict:
    t = TEXT[locale]
    gate_card = card(
        "R3-0",
        t,
        view,
        code="yellow",
        amount="¥480.00",
        payee=t["new_payee"],
        reason=t["tier_reason_yellow"],
        notes=[],
    )
    options_verified = [{"value": v, "label": t[v]} for v in ("yes", "no", "unsure")]
    options_guided = [{"value": v, "label": t[v]} for v in ("no", "yes")]
    gate = {
        "version": "Zk2pQx7Lr0aBcD3e",
        "title": t["gate_title"],
        "subtitle": t["gate_subtitle"],
        "facts_title": t["facts_title"],
        "facts": [t["gate_fact"]],
        "cues": t["cues"],
        "questions_title": t["questions_title"],
        "questions": [
            {
                "name": "verified",
                "label": t["q_verified"],
                "options": options_verified,
                "answer": None,
            },
            {"name": "guided", "label": t["q_guided"], "options": options_guided, "answer": "yes"},
        ],
        "decision": "pause",
        "locked": True,
        "preset": True,
        "interactive": True,
        "pause": {
            "title": t["pause_title"],
            "copy": t["pause_copy"],
            "retry_label": t["retry"],
            "self_report": t["self_report"],
            "cancel_hint": t["pause_cancel"],
        },
        "notice": None,
        "self_report_note": t["self_report"],
        "footer": t["footer_gate"],
    }
    if view == "judge":
        gate["judge"] = {
            "note": t["judge_note"],
            "record": t["judge_record"],
            "preset_note": t["preset_note"],
        }
    tasks = [
        task(
            "TR3",
            "R3",
            t,
            view,
            cards=[with_key(gate_card, "TR3-0")],
            step="gate_blocked",
            step_text=t["step_gate_blocked"],
            gesture=None,
            gate_notice={"text": t["gate_notice"]},
        )
    ]
    responses = {
        "R3": response(
            "R3",
            t,
            view,
            status="awaiting_input",
            status_text=t["awaiting"],
            copy=[t["pause_copy"]],
            tier_code="yellow",
            cards=[gate_card],
            device_task="TR3",
            gate=gate,
        )
    }
    state = app_state(
        t,
        view,
        locale,
        revision=5,
        caps=capabilities(),
        turns=turns_for("R3", t["user_directed"], t["pause_copy"]),
        responses=responses,
        current="R3",
        tasks=tasks,
        evidence_vm=evidence(t, rkey="R3", outcome="Escalate", tier_name="yellow", proposal=False)
        if view == "judge"
        else None,
    )
    if view == "judge":
        state["evidence"]["gate_record"] = t["judge_record"]
    return state


def phrase_gate(locale: str, view: str = "customer") -> dict:
    t = TEXT[locale]
    judge = {"phrase_gate_note": t["phrase_judge"]} if view == "judge" else None
    responses = {
        "R1": response(
            "R1",
            t,
            view,
            status="phrase_gate",
            status_text=t["status_phrase"],
            copy=[t["phrase_gate"]],
            tier_code=None,
            cards=[],
            device_task=None,
            cancel=False,
            extra_judge=judge,
        )
    }
    return app_state(
        t,
        view,
        locale,
        revision=3,
        caps=capabilities(),
        turns=turns_for("R1", t["user_held"], t["phrase_gate"]),
        responses=responses,
        current="R1",
        tasks=[],
    )


def clarify(locale: str, view: str = "customer") -> dict:
    t = TEXT[locale]
    clarify_vm = {
        "label": t["clarify_label"],
        "choices": [{"key": "c0", "label": t["lin_a"]}, {"key": "c1", "label": t["lin_b"]}],
        "chosen": None,
        "review_label": t["review"],
        "locked": False,
        "locked_text": None,
        "error_text": None,
    }
    if view == "judge":
        clarify_vm["evidence_lines"] = ["contact_ref candidates: 2"]
    responses = {
        "R1": response(
            "R1",
            t,
            view,
            status="needs_clarification",
            status_text=t["status_clarify"],
            copy=[t["clarify_label"]],
            tier_code=None,
            cards=[],
            device_task=None,
            clarify=clarify_vm,
        )
    }
    return app_state(
        t,
        view,
        locale,
        revision=3,
        caps=capabilities(),
        turns=turns_for("R1", t["user_clarify"], t["clarify_label"]),
        responses=responses,
        current="R1",
        tasks=[],
    )


def plan_subset(locale: str, view: str = "judge") -> dict:
    t = TEXT[locale]
    amounts = ["¥268.00", "¥199.00", "¥500.00", "¥1,299.00"]
    node_cards = [
        card(
            f"R4-{i}",
            t,
            view,
            code="yellow",
            amount=amounts[i],
            payee=label,
            reason=t["tier_reason_yellow"],
            notes=[],
            title=t["plan_card"],
            purpose=t["plan_purpose"],
            position=f"{i + 1}/4",
        )
        for i, label in enumerate(t["plan_nodes"])
    ]
    select = {
        "tkey": "TR4",
        "rule_text": t["plan_rule"],
        "nodes": [
            {"key": f"n{i}", "label": label, "checked": i < 2}
            for i, label in enumerate(t["plan_nodes"])
        ],
        "mode": "subset",
        "count": 2,
        "total": 4,
        "error_text": None,
    }
    gesture = {
        "op": "confirm",
        "token": "Pq7dLm2xZ0aR5tVb8nC1wE",
        "card_version": "1a2b3c4d5e6f70811a2b3c4d5e6f7081",
        "label": t["make_card"],
    }
    tasks = [
        task(
            "TR4",
            "R4",
            t,
            view,
            cards=[with_key(c, f"TR4-{i}") for i, c in enumerate(node_cards[:2])],
            step="confirm_subset",
            step_text=t["step_subset"],
            gesture=gesture,
            selection={"count": 2, "total": 4, "text": t["selection"]},
            nodes_version="0f1e2d3c4b5a69780f1e2d3c4b5a6978",
        )
    ]
    responses = {
        "R4": response(
            "R4",
            t,
            view,
            status="awaiting_selection",
            status_text=t["status_plan"],
            copy=[t["plan_rule"]],
            tier_code="yellow",
            cards=node_cards,
            device_task="TR4",
            plan_select=select,
        )
    }
    caps = capabilities(
        tabs=["plans"],
        shortcuts=["transfer", "cards", "birthday", "scam_drill"],
        plan_select=True,
    )
    return app_state(
        t,
        view,
        locale,
        revision=9,
        caps=caps,
        turns=turns_for("R4", t["user_birthday"], t["plan_rule"]),
        responses=responses,
        current="R4",
        tasks=tasks,
        evidence_vm=evidence(t, rkey="R4", outcome="Escalate", tier_name="yellow", proposal=False)
        if view == "judge"
        else None,
    )


def locked(locale: str, view: str = "judge") -> dict:
    t = TEXT[locale]
    red_card = card(
        "R2-0",
        t,
        view,
        code="red",
        amount="¥5,000.00",
        payee=t["mom"],
        reason=t["tier_reason_red"],
        notes=[{"kind": "red", "title": None, "lines": [t["red_note"]], "footer": None}],
    )
    tasks = [
        task(
            "TR2",
            "R2",
            t,
            view,
            cards=[with_key(red_card, "TR2-0")],
            step="account_locked",
            step_text=t["step_locked"],
            gesture=None,
        )
    ]
    responses = {
        "R2": response(
            "R2",
            t,
            view,
            status="rejected",
            status_text=t["status_failed"],
            copy=[t["copy_locked"]],
            tier_code="red",
            cards=[red_card],
            device_task="TR2",
        )
    }
    return app_state(
        t,
        view,
        locale,
        revision=14,
        caps=capabilities(),
        turns=turns_for("R2", t["user_red"], t["copy_locked"]),
        responses=responses,
        current="R2",
        tasks=tasks,
        locked=True,
        evidence_vm=evidence(
            t,
            rkey="R2",
            outcome="Escalate",
            tier_name="red",
            proposal=True,
            safety_state="Locked",
            reason="breaker_mfa",
        )
        if view == "judge"
        else None,
    )


def fresh(locale: str, view: str = "customer") -> dict:
    t = TEXT[locale]
    return app_state(
        t,
        view,
        locale,
        revision=0,
        caps=capabilities(),
        turns=[],
        responses={},
        current=None,
        tasks=[],
        generation=NEW_GENERATION,
        logged_in=False,
    )


def build() -> dict[str, dict]:
    out = {}
    for locale, suffix in (("zh-CN", ""), ("en", "-en")):
        t = TEXT[locale]
        out[f"customer-red-code{suffix}"] = ok(red_code(locale))
        out[f"judge-gate-paused-preset{suffix}"] = ok(gate_paused_preset(locale))
        out[f"customer-phrase-gate{suffix}"] = ok(phrase_gate(locale))
        out[f"judge-locked{suffix}"] = ok(locked(locale))
        out[f"customer-clarify{suffix}"] = ok(clarify(locale))
        out[f"judge-plan-subset{suffix}"] = ok(plan_subset(locale))
        out[f"err-card-changed{suffix}"] = err(
            "card_changed", t["card_changed"], red_code(locale, revision=9)
        )
        out[f"err-stale-generation{suffix}"] = err("stale_generation", t["stale"], fresh(locale))
        # Extra views of the same scenarios, for component specs.
        out[f"judge-red-code{suffix}"] = ok(red_code(locale, "judge"))
        out[f"customer-gate-paused-preset{suffix}"] = ok(gate_paused_preset(locale, "customer"))
        out[f"judge-phrase-gate{suffix}"] = ok(phrase_gate(locale, "judge"))
        out[f"customer-locked{suffix}"] = ok(locked(locale, "customer"))
        out[f"customer-login{suffix}"] = ok({**fresh(locale), "generation": GENERATION})
    return out


# --- checker -------------------------------------------------------------------------------
# A type is: a name in TYPES, a primitive name, or a tuple
#   ("list", T) ("map", T) ("null", T) ("enum", *values) ("lit", v) ("re", pattern).
# A struct is {key: T}; key suffix "?" = may be absent, prefix "J:" = judge view only.

KEY_RE = {
    "tab": r"^[A-Za-z0-9_-]{32}$",
    "generation": r"^[A-Za-z0-9_-]{12}$",
    "rkey": r"^R[1-9][0-9]{0,5}$",
    "tkey": r"^T(R[1-9][0-9]{0,5}|BOOL[1-9][0-9]{0,5})$",
    "choice": r"^c(0|[1-9][0-9]{0,3})$",
    "node": r"^n(0|[1-9][0-9]{0,2})$",
    "gate_version": r"^[A-Za-z0-9_-]{16}$",
    "gesture": r"^[A-Za-z0-9_-]{22}$",
    "card_version": r"^[0-9a-f]{32}$",
}
STR, INT, BOOL, OBJ = "str", "int", "bool", "object"
TIER = "TierVM"
STEPS = (
    "confirm",
    "confirm_intent",
    "mfa_request",
    "mfa_code",
    "final_confirm",
    "confirm_plan",
    "confirm_subset",
    "select_nodes",
    "not_current",
    "gate_blocked",
    "gate_unevaluated",
    "closed_facts",
    "closed_challenge",
    "account_locked",
    "closed_failed",
    "closed_unknown",
    "withdrawn",
    "refused",
    "done",
    "unknown",
)
SHORTCUT = (
    "enum",
    "transfer",
    "billing",
    "wealth",
    "cards",
    "subscriptions",
    "birthday",
    "scam_drill",
)
LINES = ("list", STR)

TYPES: dict[str, dict] = {
    "TierVM": {
        "code": ("enum", "green", "yellow", "red", "black"),
        "label": STR,
        "glyph": ("enum", "circle", "diamond", "triangle", "square"),
    },
    "HashVM": {
        "algorithm": ("lit", "SHA-256(JCS)"),
        "short": ("re", r"^[0-9a-f]{16}$"),
        "full": ("re", r"^[0-9a-f]{64}$"),
    },
    "Notice": {"id": STR, "level": ("enum", "info", "success", "warning", "error"), "text": STR},
    "AppState": {
        "schema": ("lit", 1),
        "contract": ("lit", "web-contract/1.1"),
        "generation": ("re", KEY_RE["generation"]),
        "revision": INT,
        "view": ("enum", "customer", "judge"),
        "locale": ("enum", "zh-CN", "en"),
        "capabilities": "Capabilities",
        "session": "SessionVM",
        "shell": "ShellVM",
        "login?": "LoginVM",
        "chat": "ChatVM",
        "device": "DeviceVM",
        "J:evidence": "EvidenceVM",
    },
    "Capabilities": {
        "tabs": ("list", ("enum", "accounts", "wealth", "plans")),
        "shortcuts": ("list", SHORTCUT),
        "live": BOOL,
        "plan_select": BOOL,
        "compensate": BOOL,
        "aa": BOOL,
        "lab": BOOL,
    },
    "SessionVM": {
        "logged_in": BOOL,
        "clock_text": STR,
        "bank_name": STR,
        "assistant_name": STR,
        "phone_url": (
            "re",
            r"^/phone/[A-Za-z0-9_-]{16,64}\?lang=(zh-CN|en)&view="
            r"(customer|judge)$",
        ),
    },
    "LoginVM": {"title": STR, "intro": STR, "button": STR, "caption": STR},
    "ShellVM": {
        "corner": STR,
        "J:judge_strip": STR,
        "badge": {"kind": ("enum", "demo", "model"), "text": STR},
        "safety": "SafetyVM",
        "paused_banner": ("null", STR),
        "notices": ("list", "Notice"),
    },
    "SafetyVM": {
        "locked": BOOL,
        "text": ("null", STR),
        "hint": ("null", STR),
        "J:state": ("enum", "Normal", "Watch", "Locked"),
        "J:reason": ("null", STR),
        "J:unlock_after_text": ("null", STR),
        "J:rule_ids": LINES,
    },
    "ChatVM": {
        "shortcuts": ("list", {"key": SHORTCUT, "label": STR}),
        "drill_caption": ("null", STR),
        "greeting": ("null", STR),
        "turns": ("list", "Turn"),
        "responses": ("map", "ResponseVM"),
        "current": ("null", ("re", KEY_RE["rkey"])),
        "gated_elsewhere": ("list", {"rkey": ("re", KEY_RE["rkey"]), "label": STR}),
        "task_panel": ("null", "TaskPanelVM"),
        "composer": {
            "placeholder": STR,
            "max_chars": ("lit", 4000),
            "attach_label": STR,
            "attach_note": STR,
            "contacts_note": STR,
            "disabled": BOOL,
        },
    },
    "Turn": {
        "id": STR,
        "role": ("enum", "user", "assistant", "system"),
        "rkey?": ("re", KEY_RE["rkey"]),
        "text": STR,
        "attachment_note?": STR,
        "remark_note?": STR,
    },
    "ResponseVM": {
        "rkey": ("re", KEY_RE["rkey"]),
        "current": BOOL,
        "status": STR,
        "status_text": STR,
        "copy_lines": LINES,
        "tier": ("null", TIER),
        "requested_tier": ("null", TIER),
        "redisplay_notice": ("null", STR),
        "cards": ("list", "CardVM"),
        "plan_select?": "PlanSelectVM",
        "clarify?": "ClarifyVM",
        "aa?": "AAVM",
        "birthday?": "BirthdayVM",
        "gate": ("null", "GateVM"),
        "result": ("null", "ResultVM"),
        "failure": ("null", "FailureVM"),
        "live_notes": ("list", "Notice"),
        "device_task": ("null", ("re", KEY_RE["tkey"])),
        "route_hint": ("null", "RouteHintVM"),
        "fallback_shortcuts": BOOL,
        "cancel": ("null", {"label": STR, "hint": STR}),
        "J:card_source?": ("enum", "authoritative", "envelope"),
        "J:card_mismatch?": STR,
        "J:phrase_gate_note?": STR,
        "J:parse_note?": LINES,
        "J:technical_url?": STR,
    },
    "RouteHintVM": {
        "target": ("enum", "accounts", "wealth", "plans", "operator", "fallback"),
        "label": STR,
        "note": ("null", STR),
    },
    "ClarifyVM": {
        "label": STR,
        "choices": ("list", {"key": ("re", KEY_RE["choice"]), "label": STR}),
        "chosen": ("null", STR),
        "review_label": STR,
        "locked": BOOL,
        "locked_text": ("null", STR),
        "error_text": ("null", STR),
        "J:evidence_lines?": LINES,
    },
    "PlanSelectVM": {
        "tkey": ("re", KEY_RE["tkey"]),
        "rule_text": STR,
        "nodes": ("list", {"key": ("re", KEY_RE["node"]), "label": STR, "checked": BOOL}),
        "mode": ("enum", "none", "all", "subset", "invalid"),
        "count": INT,
        "total": INT,
        "error_text": ("null", STR),
    },
    "AAVM": {"prompt": STR, "needed": INT, "choices": ("list", {"key": STR, "label": STR})},
    "BirthdayVM": {
        "lines": LINES,
        "choices": ("list", {"key": STR, "label": STR, "enabled": BOOL}),
        "error_text": ("null", STR),
    },
    "CardVM": {
        "key": STR,
        "title": STR,
        "position?": STR,
        "purpose": STR,
        "amount_text": STR,
        "tier": TIER,
        "tier_reason": STR,
        "facts": ("list", "FactVM"),
        "notes": ("list", "NoteVM"),
        "not_signed_label": STR,
        "why": {"lines": LINES},
        "J:technical?": {"card": OBJ, "amount_raw": ("null", STR), "contact_aliases": LINES},
        "J:structured?": ("list", {"label": STR, "lines": LINES}),
    },
    "FactVM": {"key": ("re", r"^[a-z_][a-z0-9_.]*$"), "label": STR, "value": STR},
    "NoteVM": {
        "kind": (
            "enum",
            "suitability",
            "fraud",
            "recipient_context",
            "irreversible",
            "red",
            "schedule_scope",
        ),
        "title": ("null", STR),
        "lines": LINES,
        "footer": ("null", STR),
    },
    "GateVM": {
        "version": ("re", KEY_RE["gate_version"]),
        "title": STR,
        "subtitle": STR,
        "facts_title": STR,
        "facts": LINES,
        "cues": ("null", STR),
        "questions_title": STR,
        "questions": (
            "list",
            {
                "name": ("enum", "verified", "guided"),
                "label": STR,
                "options": ("list", {"value": STR, "label": STR}),
                "answer": ("null", STR),
            },
        ),
        "decision": ("enum", "incomplete", "pause", "proceed", "stale"),
        "locked": BOOL,
        "preset": BOOL,
        "interactive": BOOL,
        "pause": (
            "null",
            {"title": STR, "copy": STR, "retry_label": STR, "self_report": STR, "cancel_hint": STR},
        ),
        "notice": ("null", STR),
        "self_report_note": STR,
        "footer": STR,
        "J:judge?": {"note": STR, "record": STR, "preset_note": ("null", STR)},
    },
    "ResultVM": {
        "banner": ("null", {"tone": ("enum", "success", "info", "warning", "error"), "text": STR}),
        "receipt_line": ("null", STR),
        "nodes?": ("list", "ResultVM"),
        "facts_lines": LINES,
        "message_lines": LINES,
        "why": {"lines": LINES},
        "bill?": OBJ,
        "subscriptions?": OBJ,
        "transactions?": {"columns": LINES, "rows": ("list", LINES)},
        "stop_receipt_lines?": LINES,
        "J:rule_ids?": LINES,
        "J:receipt?": ("null", OBJ),
        "J:audit_checkpoint?": ("null", OBJ),
    },
    "FailureVM": {"kind": ("enum", "failed", "unknown"), "lines": LINES, "cancel_hint": STR},
    "TaskPanelVM": {
        "route_label": STR,
        "tier": ("null", TIER),
        "copy": STR,
        "requested_hint": ("null", STR),
        "dag": OBJ,
        "controls": {"stop": STR, "compensate": ("null", STR), "handoff": STR},
    },
    "DeviceVM": {
        "title": STR,
        "idle_text": STR,
        "queue_text": ("null", STR),
        "tasks": ("list", "DeviceTaskVM"),
        "active": ("null", ("re", KEY_RE["tkey"])),
        "sms": {"url": STR, "label": STR, "open_label": STR, "footer": STR},
    },
    "DeviceTaskVM": {
        "key": ("re", KEY_RE["tkey"]),
        "source": ("enum", "chat", "wealth", "subscriptions", "plans", "compensation"),
        "source_label": STR,
        "rkey?": ("re", KEY_RE["rkey"]),
        "current": BOOL,
        "cards": ("list", "CardVM"),
        "selection?": {"count": INT, "total": INT, "text": STR},
        "step": ("enum", *STEPS),
        "step_text": STR,
        "gate_notice?": {"text": STR},
        "back?": {"rkey": ("re", KEY_RE["rkey"]), "label": STR},
        "mfa?": {"sent_text": STR, "method_text": STR, "hint": STR, "expired_text": ("null", STR)},
        "gesture": (
            "null",
            {
                "op": ("enum", "confirm", "mfa_request", "mfa_verify"),
                "token": ("re", KEY_RE["gesture"]),
                "card_version": ("re", KEY_RE["card_version"]),
                "label": STR,
            },
        ),
        "nodes_version?": ("re", KEY_RE["card_version"]),
        "failure?": "FailureVM",
        "result?": "ResultVM",
        "cancel": ("null", {"label": STR, "hint": STR}),
        "J:hash?": {
            "algorithm": ("lit", "SHA-256(JCS)"),
            "short": STR,
            "full": STR,
            "status": ("enum", "pending", "signed", "not_yet"),
            "label": STR,
        },
    },
    "EvidenceVM": {
        "open_by_default": ("list", ("enum", "kernel", "consistency", "dispatch")),
        "planner": {"badge": STR, "source": STR, "counters": LINES},
        "response?": {
            "rkey": ("re", KEY_RE["rkey"]),
            "intent": STR,
            "status": STR,
            "rule_ids": LINES,
            "rule_lines": LINES,
            "audit_hash": "HashVM",
        },
        "kernel?": {
            "outcome": ("null", ("enum", "Accept", "Escalate", "Reject")),
            "tier": ("null", STR),
            "rule_ids": LINES,
            "rule_lines": LINES,
            "note": ("null", STR),
        },
        "consistency?": {
            "display_card": "HashVM",
            "proposal_card": ("null", "HashVM"),
            "same_hash": ("null", BOOL),
            "same_business_facts": ("null", BOOL),
            "action": ("null", "HashVM"),
            "cert": ("null", "HashVM"),
            "card_core": ("null", "HashVM"),
            "challenge": ("enum", "none", "issued", "verified"),
            "not_yet_text": ("null", STR),
            "mismatch_text": ("null", STR),
        },
        "dispatch": {
            "count": INT,
            "receipts": ("list", {"id": STR, "kind": STR, "status": STR, "time_text": STR}),
        },
        "audit": {"count": INT, "head": "HashVM", "verified": BOOL, "note": STR},
        "safety": {"state": STR, "reason": ("null", STR)},
        "gate_record?": STR,
        "intent_log": ("list", {"intent": STR, "status": STR, "response": STR, "utterance": STR}),
        "intent_log_note": STR,
        "planner_log": (
            "list",
            {
                "rkey": STR,
                "planner": STR,
                "intent": STR,
                "outcome": ("null", STR),
                "tier": ("null", STR),
                "rule_ids": LINES,
            },
        ),
        "footer": STR,
    },
}

ERROR_CODES = {
    "bad_json",
    "unknown_field",
    "no_session",
    "forbidden_origin",
    "not_found",
    "method_not_allowed",
    "stale_generation",
    "not_current",
    "gesture_unknown",
    "gesture_mismatch",
    "card_changed",
    "gesture_closed",
    "facts_incomplete",
    "gate_stale",
    "not_applicable",
    "msg_id_conflict",
    "not_at_boundary",
    "live_on",
    "live_unavailable",
    "too_large",
    "invalid_input",
    "operation_failed",
    "outcome_unknown",
    "internal",
    "busy",
}


def check_value(value, spec, path: str, view: str, problems: list[str]) -> None:
    if isinstance(spec, str) and spec in TYPES:
        check_struct(value, TYPES[spec], path, view, problems)
    elif isinstance(spec, dict):
        check_struct(value, spec, path, view, problems)
    elif spec == STR:
        if not isinstance(value, str):
            problems.append(f"{path}: expected string")
    elif spec == INT:
        if not isinstance(value, int) or isinstance(value, bool):
            problems.append(f"{path}: expected integer")
    elif spec == BOOL:
        if not isinstance(value, bool):
            problems.append(f"{path}: expected boolean")
    elif spec == OBJ:
        if not isinstance(value, dict):
            problems.append(f"{path}: expected object")
    elif spec[0] == "list":
        if not isinstance(value, list):
            problems.append(f"{path}: expected list")
            return
        for index, item in enumerate(value):
            check_value(item, spec[1], f"{path}[{index}]", view, problems)
    elif spec[0] == "map":
        if not isinstance(value, dict):
            problems.append(f"{path}: expected map")
            return
        for key, item in value.items():
            check_value(item, spec[1], f"{path}.{key}", view, problems)
    elif spec[0] == "null":
        if value is not None:
            check_value(value, spec[1], path, view, problems)
    elif spec[0] == "enum":
        if value not in spec[1:]:
            problems.append(f"{path}: {value!r} not in {spec[1:]}")
    elif spec[0] == "lit":
        if value != spec[1]:
            problems.append(f"{path}: expected {spec[1]!r}")
    elif spec[0] == "re":
        if not isinstance(value, str) or not re.fullmatch(spec[1], value):
            problems.append(f"{path}: {value!r} does not match {spec[1]}")
    else:
        problems.append(f"{path}: unknown spec {spec!r}")


def check_struct(value, struct: dict, path: str, view: str, problems: list[str]) -> None:
    if not isinstance(value, dict):
        problems.append(f"{path}: expected object")
        return
    known = set()
    for raw, spec in struct.items():
        judge_only = raw.startswith("J:")
        optional = raw.endswith("?")
        key = raw.removeprefix("J:").removesuffix("?")
        known.add(key)
        if judge_only and view == "customer":
            if key in value:
                problems.append(f"{path}.{key}: judge-only key present in the customer view")
            continue
        if key not in value:
            if not optional:
                problems.append(f"{path}.{key}: missing")
            continue
        check_value(value[key], spec, f"{path}.{key}", view, problems)
    for key in value:
        if key not in known:
            problems.append(f"{path}.{key}: not in the contract")


def float_free(value, path: str, problems: list[str]) -> None:
    if isinstance(value, float):
        problems.append(f"{path}: float")
    elif isinstance(value, dict):
        for key, item in value.items():
            float_free(item, f"{path}.{key}", problems)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            float_free(item, f"{path}[{index}]", problems)


def semantic(state: dict, problems: list[str]) -> None:
    chat, tasks = state["chat"], state["device"]["tasks"]
    rkeys = {turn.get("rkey") for turn in chat["turns"] if turn.get("rkey")}
    if set(chat["responses"]) - rkeys:
        problems.append("chat.responses: rkey not in turns")
    for rkey, vm in chat["responses"].items():
        if vm["rkey"] != rkey:
            problems.append(f"chat.responses.{rkey}: rkey mismatch")
        for card_vm in vm["cards"]:
            if any(
                note["kind"] in {f["key"] for f in card_vm["facts"]} for note in card_vm["notes"]
            ):
                problems.append(f"chat.responses.{rkey}: note inside facts")
    by_key = {item["key"]: item for item in tasks}
    if tasks and state["device"]["active"] != tasks[0]["key"]:
        problems.append("device.active must be the first task")
    for item in tasks:
        if item["gesture"] is not None and item["step"] not in STEPS[:7]:
            problems.append(f"device.tasks.{item['key']}: gesture on a display-only step")
        rkey = item.get("rkey")
        if rkey and rkey in chat["responses"]:
            phone_cards = chat["responses"][rkey]["cards"]
            for card_vm in item["cards"]:
                twin = [
                    c
                    for c in phone_cards
                    if {k: v for k, v in c.items() if k != "key"}
                    == {k: v for k, v in card_vm.items() if k != "key"}
                ]
                if not twin:
                    problems.append(f"device.tasks.{item['key']}: card differs from the phone")
    for vm in chat["responses"].values():
        if vm["device_task"] and vm["device_task"] not in by_key:
            problems.append(f"chat.responses.{vm['rkey']}: device_task not listed")
    if state["view"] == "customer":
        text = json.dumps(state, ensure_ascii=False)
        if re.search(r"[0-9a-f]{64}", text):
            problems.append("customer view carries a 64-hex value")
        for word in ("LIVE_", "LLM_", "CORRIDOR_", "scripted", "APR-", "BRK-"):
            if word in text:
                problems.append(f"customer view carries {word!r}")
    if state.get("login") is not None and (chat["turns"] or tasks):
        problems.append("login present while turns or tasks are not empty")


def check_response(body: dict) -> list[str]:
    problems: list[str] = []
    float_free(body, "$", problems)
    if body.get("ok") is True:
        state = body.get("state")
        if not isinstance(state, dict):
            return ["$.state: missing"]
        allowed = {"ok", "generation", "revision", "state", "tab", "rkey", "result"}
        problems += [f"$.{key}: not in the contract" for key in body if key not in allowed]
    elif body.get("ok") is False:
        error = body.get("error")
        if not isinstance(error, dict) or error.get("code") not in ERROR_CODES:
            problems.append("$.error: bad code")
        elif set(error) != {"code", "message", "retry"}:
            problems.append("$.error: keys")
        state = body.get("state")
        if state is None:
            return problems
    else:
        return ["$.ok: missing"]
    if body.get("generation") != state.get("generation") or body.get("revision") != state.get(
        "revision"
    ):
        problems.append("$: generation/revision differ from the state's")
    view = state.get("view", "customer")
    check_value(state, "AppState", "$.state", view, problems)
    if not problems:
        semantic(state, problems)
    return problems


def contracts_validator():
    """The B2 validator, when ``corridor.web.contracts`` provides one."""
    try:
        from corridor.web import contracts
    except ImportError:
        return None
    for name in ("validate_response", "check_response", "validate"):
        function = getattr(contracts, name, None)
        if callable(function):
            return function
    return None


def check(directory: Path = FIXTURES) -> list[str]:
    problems = []
    names = sorted(path.name for path in directory.glob("*.json"))
    required = [
        "customer-red-code",
        "judge-gate-paused-preset",
        "customer-phrase-gate",
        "judge-locked",
        "customer-clarify",
        "judge-plan-subset",
        "err-card-changed",
        "err-stale-generation",
    ]
    for base in required:
        for name in (f"{base}.json", f"{base}-en.json"):
            if name not in names:
                problems.append(f"{name}: missing")
    validator = contracts_validator()
    for name in names:
        body = json.loads((directory / name).read_text(encoding="utf-8"))
        problems += [f"{name} {item}" for item in check_response(body)]
        if validator is not None:
            try:
                validator(body)
            except Exception as exc:  # report every fixture, not just the first
                problems.append(f"{name} contracts.py: {type(exc).__name__}: {exc}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate or check the e2e fixtures")
    parser.add_argument("--check", action="store_true", help="check only; write nothing")
    args = parser.parse_args(argv)
    if not args.check:
        FIXTURES.mkdir(parents=True, exist_ok=True)
        for name, body in build().items():
            text = json.dumps(body, ensure_ascii=False, indent=1) + "\n"
            # LF on every platform (.gitattributes: eol=lf).
            (FIXTURES / f"{name}.json").write_text(text, encoding="utf-8", newline="\n")
    problems = check()
    for problem in problems:
        print(problem)
    print(f"fixtures: {len(list(FIXTURES.glob('*.json')))} files, {len(problems)} problems")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT / "src"))
    sys.exit(main())
