from copy import deepcopy
from fractions import Fraction
from hashlib import sha256
import json,re,time
from acceptance.http_support import Browser,serve

class ScenarioFailure(RuntimeError):
    pass

def plain(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [plain(v) for v in value]
    if isinstance(value, Fraction):
        return str(value)
    return value

def safe_calls(client):
    fields = ("schema_version", "model", "api", "reasoning_effort", "max_output_tokens",
              "prompt_hash", "tool_state_hash", "request_hash", "output_sha256", "source",
              "failed", "error_type", "usage", "cost_usd")
    return [{key: row[key] for key in fields if key in row} for row in client.calls]

def semantic_projection(value):
    receipts = []
    for row in value["receipts"]:
        effects = []
        for effect in row.get("exact_effects", []):
            if isinstance(effect, dict):
                effects.append({key: effect.get(key) for key in
                                ("effect_type", "object_ref", "counterparty_ref", "amount", "field", "before", "after")
                                if key in effect and not isinstance(effect.get(key), dict)})
        receipts.append({key: row.get(key) for key in ("receipt_type", "kind", "status", "function_id") if key in row} | {"effects": effects})
    effects = value["state_effects"]
    return {
        "response": {key: (value.get("response") or {}).get(key) for key in ("status", "status_text")},
        "bank_execute_delta": value["bank_execute_delta"],
        "receipts": receipts,
        "tasks": [{key: row.get(key) for key in ("source", "step", "gesture_op", "card_kind")}
                  for row in value["tasks"]],
        "state_effects": {
            "positions": effects["positions"],
            "subscription_statuses": effects["subscription_statuses"],
            "card_statuses": effects["card_statuses"],
            "plans": [{key: plan[key] for key in ("blocked", "node_tools", "node_statuses")}
                      for plan in effects["plans"]],
        },
        "accepted_terminal_assertions": value["accepted_terminal_assertions"],
    }

def semantic_hash(value):
    encoded = json.dumps(semantic_projection(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(encoded.encode("utf-8")).hexdigest()

def route(path: str) -> str:
    path = path.split("?", 1)[0]
    path = re.sub(r"/api/chat/responses/[^/]+", "/api/chat/responses/{rkey}", path)
    path = re.sub(r"/api/device/tasks/[^/]+", "/api/device/tasks/{tkey}", path)
    path = re.sub(r"/api/phone/[A-Za-z0-9_-]{16,64}", "/api/phone/{pair}", path)
    return path

def instrument(page):
    page.http_steps = []
    page.device_operations = []
    original = page.client.request
    def request(method, path, body, headers, *, read=True):
        response = original(method, path, body, headers, read=read)
        page.http_steps.append({"method": method, "path": route(path),
                                "status": None if response is None else response.code})
        return response
    page.client.request = request

def response_summary(page, rkey):
    vm = page.response(rkey)
    return {key: vm.get(key) for key in ("status", "status_text", "intent", "tier", "result") if key in vm}

def tasks_summary(page):
    out = []
    for item in page.state.get("device", {}).get("tasks", []):
        gesture = item.get("gesture") or {}
        out.append({key: item.get(key) for key in ("key", "source", "step", "status") if key in item})
        out[-1]["gesture_op"] = gesture.get("op")
        out[-1]["card_kind"] = item.get("card", {}).get("kind") if isinstance(item.get("card"), dict) else None
    return out

def do_device(page, tkey, op, **extra):
    task = next((x for x in page.state.get("device", {}).get("tasks", []) if x.get("key") == tkey), None)
    if task is None or not task.get("gesture") or task["gesture"].get("op") != op:
        raise ScenarioFailure("expected_device_gesture_missing")
    before = page.ws.controller.gateway.bank.execute_count
    result = page.device(tkey, op, **extra)
    page.device_operations.append({"task": tkey, "step": task.get("step"), "op": op,
                                   "bank_execute_delta": page.ws.controller.gateway.bank.execute_count - before,
                                   "result_status": (result.get("result") or {}).get("status")})
    return result

def finish_task(page, tkey):
    for _ in range(8):
        task = next((x for x in page.state.get("device", {}).get("tasks", []) if x.get("key") == tkey), None)
        if task is None or not task.get("gesture"):
            return task
        op = task["gesture"].get("op")
        if op == "mfa_verify":
            code = page.sms_code()
            if not code:
                raise ScenarioFailure("synthetic_phone_code_missing")
            do_device(page, tkey, op, code=code)
        elif op in {"confirm", "mfa_request"}:
            do_device(page, tkey, op)
        else:
            raise ScenarioFailure("unexpected_device_operation")
    raise ScenarioFailure("device_step_limit")

def run_flow(page, index):
    text = SCENARIOS[index][1]
    before = page.ws.controller.gateway.bank.execute_count
    rkey = page.send(text)
    if index == 1:
        vm = page.response(rkey)
        if vm["status"] != "settled" or vm.get("device_task") is not None:
            raise ScenarioFailure("bill_analysis_not_settled_without_device")
    elif index == 4:
        vm = page.response(rkey)
        selection = vm.get("subscription_select")
        if not selection or not selection.get("choices"):
            raise ScenarioFailure("subscription_choice_missing")
        raw_before_choice = page.ws.session.response(page.ws.rkeys[rkey])
        chosen_ref = (raw_before_choice.get("payload") or {}).get("subscription_choices", [None])[0]
        before_choice = page.ws.controller.gateway.bank.execute_count
        chosen = page.post(f"/api/chat/responses/{rkey}/subscription",
                           {"choice": "c0", "list_version": selection["list_version"]})
        choice_delta = page.ws.controller.gateway.bank.execute_count - before_choice
        page.device_operations.append({"op": "subscription_choice", "bank_execute_delta": choice_delta})
        if choice_delta != 0:
            raise ScenarioFailure("subscription_choice_executed_before_device")
        rkey = chosen["rkey"]
        card = page.ws.session.response(page.ws.rkeys[rkey]).get("card")
        if not card or card.get("kind") != "cancel_subscription":
            raise ScenarioFailure("subscription_choice_did_not_prepare_cancel_card")
        if (card.get("objects") or {}).get("authorization_ref") != chosen_ref:
            raise ScenarioFailure("subscription_choice_ref_mismatch")
        finish_task(page, "T" + rkey)
    elif index == 5:
        vm = page.response(rkey)
        if vm.get("birthday") and vm["birthday"].get("choices"):
            keys = {row["key"] for row in vm["birthday"]["choices"]}
            if "c1" not in keys:
                raise ScenarioFailure("expected_birthday_choice_c1_missing")
            page.post(f"/api/chat/responses/{rkey}/birthday", {"choice": "c1"})
        plan = next((x for x in page.state["device"]["tasks"] if x.get("key") == "T" + rkey), None)
        if not plan:
            raise ScenarioFailure("birthday_plan_task_missing")
        tkey = plan["key"]
        version = page.task(tkey)["nodes_version"]
        before_nodes = page.ws.controller.gateway.bank.execute_count
        page.post(f"/api/device/tasks/{tkey}/nodes", {"card_version": version,
                  "nodes": ["n0", "n1", "n2", "n3"]})
        page.device_operations.append({"op": "select_plan_nodes",
                                       "bank_execute_delta": page.ws.controller.gateway.bank.execute_count - before_nodes})
        if page.device_operations[-1]["bank_execute_delta"] != 0:
            raise ScenarioFailure("birthday_node_selection_executed")
        finish_task(page, tkey)
        page.send("停止")
        refund = page.send("退回锁定的预算")
        refund_task = "T" + refund
        if page.task(refund_task)["step"] != "confirm_intent":
            raise ScenarioFailure("birthday_refund_card_missing")
        finish_task(page, refund_task)
    elif index == 2:
        raw = page.ws.session.response(page.ws.rkeys[rkey])
        purchase = (raw.get("payload") or {}).get("purchase") or {}
        if raw.get("intent") != "investment" or raw.get("status") != "awaiting_input":
            raise ScenarioFailure("wealth_review_not_awaiting_input")
        if purchase.get("product_ref") != "P_A" or purchase.get("amount") != "2000/1":
            raise ScenarioFailure("wealth_review_facts_not_P_A_2000")
        buttons = raw.get("buttons") or []
        if [button.get("id") for button in buttons] != ["confirm"] or buttons[0].get("response_id") != raw.get("response_id"):
            raise ScenarioFailure("wealth_review_confirmation_button_mismatch")
        tkey = "T" + rkey
        if not any(task.get("key") == tkey for task in page.state.get("device", {}).get("tasks", [])):
            raise ScenarioFailure("wealth_review_has_no_executable_http_task")
        do_device(page, tkey, "confirm")
        prepared = page.ws.session.response(page.ws.rkeys[rkey])
        card = prepared.get("card") or {}
        if prepared.get("status") != "waiting_mfa" or card.get("kind") != "purchase_product" or card.get("tier") != "red" or card.get("amount") != "2000/1" or (card.get("objects") or {}).get("product_ref") != "P_A":
            raise ScenarioFailure("wealth_red_card_facts_mismatch_after_review")
        finish_task(page, tkey)
    elif index == 0 or index == 3:
        vm = page.response(rkey)
        if vm.get("status") in {"clarify", "refused", "error"}:
            raise ScenarioFailure("fixed_input_not_actionable")
        if index == 0 and (vm.get("tier") or {}).get("code") != "red":
            raise ScenarioFailure("transfer_1500_not_red")
        if index == 3 and page.task("T" + rkey)["step"] != "confirm_intent":
            raise ScenarioFailure("card_loss_not_confirm_intent")
        finish_task(page, "T" + rkey)
    controller = page.ws.controller
    receipts = [plain(row) for row in controller.state.receipts.values()]
    checkpoint = controller.gateway.audit.checkpoint()
    bank_delta = controller.gateway.bank.execute_count - before
    audit_valid = controller.gateway.verify_log(checkpoint)
    op_deltas = [row.get("bank_execute_delta", 0) for row in page.device_operations]
    if index == 0:
        accepted = audit_valid and bank_delta == 1 and any(
            row.get("kind") == "transfer" and row.get("status") == "settled" and
            any(effect.get("amount") == "1500/1" and
                (effect.get("object_ref") == "DEST_CONTACT_CHEN" or effect.get("counterparty_ref") == "DEST_CONTACT_CHEN")
                for effect in row.get("exact_effects", []) if isinstance(effect, dict))
            for row in receipts
        ) and op_deltas and op_deltas[-1] == 1 and all(delta == 0 for delta in op_deltas[:-1])
    elif index == 1:
        accepted = audit_valid and bank_delta == 0 and any(
            row.get("receipt_type") == "analysis" and row.get("function_id") == "bill_summary_v1" and
            (row.get("parameters") or {}).get("period") == {
                "start_inclusive": "2026-08-31T16:00:00Z", "end_exclusive": "2026-09-30T16:00:00Z"
            }
            for row in receipts
        )
    elif index == 2:
        positions = list(controller.state.positions.values())
        accepted = audit_valid and bank_delta == 1 and any(
            row.product_ref == "P_A" and str(row.principal_amount) == "2000"
            for row in positions
        ) and any(row.get("kind") == "purchase_product" and row.get("status") == "settled" for row in receipts) and op_deltas and op_deltas[-1] == 1 and all(delta == 0 for delta in op_deltas[:-1])
    elif index == 3:
        lost_effects = [
            effect for row in receipts if row.get("kind") == "report_card_lost"
            for effect in row.get("exact_effects", []) if isinstance(effect, dict)
            and effect.get("field") == "card" and (effect.get("after") or {}).get("status") == "lost"
        ]
        accepted = audit_valid and bank_delta == 1 and any(
            row.get("kind") == "report_card_lost" and row.get("status") == "settled"
            for row in receipts
        ) and len(lost_effects) == 1 and lost_effects[0]["object_ref"] == "CARD_UI_DEMO" and lost_effects[0]["object_ref"] in controller.state.cards and controller.state.cards[lost_effects[0]["object_ref"]].status == "lost" and op_deltas and op_deltas[-1] == 1 and all(delta == 0 for delta in op_deltas[:-1])
    elif index == 4:
        accepted = audit_valid and bank_delta == 1 and any(
            row.get("kind") == "cancel_subscription" and row.get("status") == "settled"
            for row in receipts
        ) and chosen_ref == "S1" and chosen_ref in controller.state.subscriptions and controller.state.subscriptions[chosen_ref].status == "cancelled" and op_deltas[0] == 0 and op_deltas[-1] == 1 and all(delta == 0 for delta in op_deltas[:-1])
    else:
        plans = controller.snapshot().get("plans", [])
        original_plan = plans[0] if plans else {}
        original_tools = [node["tool"] for node in original_plan.get("plan", {}).get("nodes", [])]
        original_statuses = [value["status"] for value in original_plan.get("nodes", {}).values()]
        accepted = audit_valid and bank_delta == 2 and all(
            any(row.get("kind") == kind and row.get("status") == "settled" for row in receipts)
            for kind in ("lock_funds", "unlock_funds")
        ) and original_tools == ["lock_funds", "unlock_funds", "merchant_order", "merchant_order"] and original_statuses == ["compensated", "cancelled", "cancelled", "cancelled"] and original_statuses[2:].count("succeeded") == 0 and sum(op_deltas) == 2 and op_deltas.count(1) == 2
    if not accepted:
        raise ScenarioFailure("scenario_terminal_assertion_failed")
    return {
        "response": response_summary(page, rkey) if rkey is not None else None,
        "bank_execute_delta": bank_delta,
        "receipts": receipts,
        "audit_valid": audit_valid,
        "audit_head": checkpoint,
        "tasks": tasks_summary(page),
        "device_operations": page.device_operations,
        "business_state": {key: plain(value) for key, value in page.ws.business.items()},
        "state_effects": {
            "positions": [{"product_ref": row.product_ref, "principal_amount": str(row.principal_amount)} for row in controller.state.positions.values()],
            "subscription_statuses": {key: value.status for key, value in controller.state.subscriptions.items()},
            "card_statuses": {key: value.status for key, value in controller.state.cards.items()},
            "plans": [
                {"plan_id": item["plan"]["plan_id"], "blocked": item["blocked"],
                 "node_tools": [node["tool"] for node in item["plan"]["nodes"]],
                 "node_statuses": [value["status"] for value in item["nodes"].values()]}
                for item in controller.snapshot().get("plans", [])
            ],
        },
        "device_operations": page.device_operations,
        "accepted_terminal_assertions": accepted,
    }

SCENARIOS = [
    ("01-transfer-1500", "给妈妈转1500元"),
    ("02-bill-september", "看看9月账单"),
    ("03-wealth-buy-p-a", "帮我申购2000元稳健30天持有"),
    ("04-card-lost", "我的卡丢了"),
    ("05-subscription-cancel", "取消订阅"),
    ("06-birthday-dag", "帮我准备老婆的生日"),
]
