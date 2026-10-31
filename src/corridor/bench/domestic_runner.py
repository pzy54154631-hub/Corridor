"""Structured Chinese scenarios and offline task interfaces, with explicit gestures."""

from hashlib import sha256
from pathlib import Path
from typing import Literal

from pydantic import Field

from corridor.agent.domestic_intent import SCENARIOS
from corridor.agent.domestic_session import DomesticSession
from corridor.agent.domestic_slots import extract_slots, trusted_slot_context
from corridor.bench.card_runner import card_state
from corridor.crypto.jcs import jcs_bytes
from corridor.kernel.rules import RULES
from corridor.schemas.bank import AnalysisReceipt
from corridor.schemas.base import Model, loads_exact
from corridor.ui.controller import DemoController, demo_state

DATA = Path(__file__).resolve().parents[3] / "data" / "scenarios" / "zh"
DeviceEvent = Literal["confirm", "mfa", "confirm_all_nodes"]


class TaskTurn(Model):
    task: str = Field(min_length=1, max_length=4000)
    simulated_device_events: list[DeviceEvent] = Field(default_factory=list, max_length=8)


class TaskRequest(TaskTurn):
    task_id: str = Field(default="DOMESTIC-TASK", min_length=1, max_length=100)
    # "family" is an additive, product-demo-only state (the interactive UI's showcase state
    # with the spouse contact and birthday); "bank" and "card" are unchanged.
    initial_state: Literal["bank", "card", "family"] = "bank"
    context: list[TaskTurn] = Field(default_factory=list, max_length=10)


def make_session(initial_state="bank", *, client=None, task_id="DOMESTIC-TASK", evidence_dir=None):
    state = demo_state()
    if initial_state == "card":
        state = state.model_copy(update={"cards": card_state().cards})
    elif initial_state == "family":
        state = demo_state(showcase=True)
    elif initial_state != "bank":
        raise ValueError("Unknown synthetic initial state")
    controller = DemoController(initial_state=state, evidence_dir=evidence_dir)
    if client is not None:
        controller.client = client
        controller.chat.client = client
    controller.start_session()
    return DomesticSession(controller, client=client, task_id=task_id)


def _rules(value):
    found = set()
    if isinstance(value, dict):
        rules = value.get("rule_ids", [])
        if isinstance(rules, list):
            found.update(item for item in rules if isinstance(item, str))
        for child in value.values():
            found.update(_rules(child))
    elif isinstance(value, list):
        for child in value:
            found.update(_rules(child))
    return found


def run_task(value: dict, *, client=None, evidence_dir=None):
    request = TaskRequest.model_validate(value)
    session = make_session(
        request.initial_state, client=client, task_id=request.task_id, evidence_dir=evidence_dir
    )
    actions, responses = [], []
    response = None
    for index, turn in enumerate([*request.context, request]):
        response = session.receive(turn.task, request_id=f"{request.task_id}:{index}")
        original = response
        for event in turn.simulated_device_events:
            if event == "mfa":
                response = session.verify_mfa(response["response_id"])
            elif event == "confirm_all_nodes":
                response = session.confirm(
                    response["response_id"],
                    selected_node_ids=[node["id"] for node in response["payload"].get("nodes", [])],
                )
            else:
                response = session.confirm(response["response_id"])
        responses.append(response)
        if (
            original["card"]
            or original["payload"].get("nodes")
            or original["intent"] in {"balance_query", "bill_report"}
        ):
            actions.append({
                "response_id": response["response_id"],
                "intent": response["intent"],
                "tier": original["tier"],
                "status": response["status"],
                "card": original["card"],
                "rule_ids": response["rule_ids"],
            })
    assert response is not None
    controller = session.controller
    records = controller.gateway.audit.records()
    result = {
        "task_id": request.task_id,
        "initial_state": request.initial_state,
        "final_status": response["status"],
        "intent": response["intent"],
        "scenario": response["scenario"],
        "tier": response["tier"],
        "needs_clarification": response["needs_clarification"],
        "actions": actions,
        "approval_events": [row for row in records if row["event"].startswith("approval_")],
        "mfa_events": [row for row in records if row["event"].startswith("mfa_")],
        "rule_ids": sorted(_rules(records) | _rules(responses)),
        "audit_hash": controller.gateway.audit.checkpoint()["head"],
        "audit_valid": controller.gateway.verify_log(),
        "dispatch_count": controller.gateway.bank.execute_count,
        "tool_calls": session.tool_calls,
        "receipts": [
            receipt.model_dump(mode="json") for receipt in controller.state.receipts.values()
        ],
        "analysis_receipts": [
            receipt.model_dump(mode="json")
            for receipt in controller.state.receipts.values()
            if isinstance(receipt, AnalysisReceipt)
        ],
        "plans": controller.snapshot()["plans"],
        "responses": responses,
        "planner_mode": session.client.mode,
        "model": session.client.model,
        "synthetic": True,
        "authentication": "simulated",
    }
    if evidence_dir is not None:
        destination = Path(evidence_dir) / "result.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        audit_path = destination.parent / "audit.jsonl"
        audit_bytes = b"".join(jcs_bytes(record) + b"\n" for record in records)
        if audit_path.exists():
            if audit_path.read_bytes() != audit_bytes:
                raise ValueError("Evidence directory contains a different retained audit")
        else:
            with audit_path.open("xb") as stream:
                stream.write(audit_bytes)
        checkpoint = controller.gateway.audit.checkpoint()
        (destination.parent / "checkpoint.json").write_bytes(jcs_bytes(checkpoint))
        destination.write_bytes(jcs_bytes(result))
        (destination.parent / "manifest.json").write_bytes(
            jcs_bytes({
                "task_id": request.task_id,
                "planner_mode": session.client.mode,
                "model": session.client.model,
                "request_hash": sha256(jcs_bytes(request)).hexdigest(),
                "files": {
                    path.name: sha256(path.read_bytes()).hexdigest()
                    for path in (destination, audit_path, destination.parent / "checkpoint.json")
                },
            })
        )
    return result


def run_zh_suite(*, output=None, client=None, task_ids=None):
    cases = loads_exact((DATA / "cases.json").read_bytes())
    for category in ("transfers", "billing", "wealth", "cards", "subscriptions", "birthday"):
        features = {case["language_feature"] for case in cases if case["category"] == category}
        if not {"ellipsis", "reference", "implicit"} <= features:
            raise ValueError("Every banking scenario needs all three Chinese language features")
    if sum(case["category"] == "out_of_scope" for case in cases) < 6:
        raise ValueError("At least six out-of-scope scenarios are required")
    if (
        sum(
            case["category"] == "sensitive" and case["request"]["initial_state"] == "card"
            for case in cases
        )
        < 6
    ):
        raise ValueError("At least six actual synthetic card/MFA scenarios are required")
    if task_ids:
        if set(task_ids) - {row["id"] for row in cases}:
            raise ValueError("Unknown Chinese scenario")
        cases = [row for row in cases if row["id"] in task_ids]
    results = []
    required = {
        "intent",
        "scenario",
        "final_status",
        "tier",
        "needs_clarification",
        "dispatch_count",
        "rule_ids",
    }
    for case in cases:
        if not required <= case["expected"].keys():
            raise ValueError("Incomplete Chinese scenario expectations")
        if set(case["expected"]["rule_ids"]) - RULES.keys():
            raise ValueError("Unknown expected kernel/gateway rule")
        result = run_task(
            {**case["request"], "task_id": case["id"]},
            client=client,
            evidence_dir=Path(output) / case["id"] if output else None,
        )
        checks = {key: result[key] == value for key, value in case["expected"].items()}
        checks["audit_valid"] = result["audit_valid"] is True
        if case["category"] == "out_of_scope":
            checks["zero_tools"] = result["tool_calls"] == 0 and not result["actions"]
        row = {
            "task_id": case["id"],
            "category": case["category"],
            "language_feature": case["language_feature"],
            "passed": all(checks.values()),
            "checks": checks,
            "actual": {key: result[key] for key in case["expected"]},
            "audit_hash": result["audit_hash"],
        }
        results.append(row)
    report = {
        "planner_mode": client.mode if client else "scripted",
        "model": client.model if client else "scripted-bank-v1",
        "total": len(results),
        "passed": sum(row["passed"] for row in results),
        "results": results,
        "live_status": "not_run" if client is None or client.mode == "scripted" else client.mode,
    }
    if output:
        Path(output).mkdir(parents=True, exist_ok=True)
        (Path(output) / "judge-zh.json").write_bytes(jcs_bytes(report))
    return report


def evaluate_intents(*, client=None, source=None):
    path = Path(source) if source else DATA / "colloquial.jsonl"
    data = path.read_bytes()
    digest = sha256(data).hexdigest()
    if (
        source is None
        and digest != (DATA / "colloquial.sha256").read_text(encoding="utf-8").split()[0]
    ):
        raise ValueError("Frozen colloquial labels do not match their checksum")
    rows = [loads_exact(line) for line in data.splitlines() if line]
    results = []
    reference = DemoController()
    trusted_context = trusted_slot_context(reference.state, reference.device.principal)
    fields = ("recipient", "amount", "date", "product")
    for row in rows:
        expected = row["expected"]
        # Every row starts from a new deployment session. The frozen context is
        # fixture setup, never carry-over from the preceding evaluation row.
        controller = DemoController()
        controller.start_session()
        session = DomesticSession(controller, client=client, task_id=row["id"])
        prior = row.get("context", {})
        session._previous_intent = prior.get("previous_intent")
        controller.chat._previous_contact = prior.get("confirmed_contact")
        controller.chat._previous_amount = prior.get("confirmed_amount")
        if prior.get("cancellable"):
            if any(ref not in controller.state.subscriptions for ref in prior["cancellable"]):
                raise ValueError("Unknown frozen prior subscription")
            session._responses["fixture-context"] = {
                "status": "settled",
                "intent": "subscription_query",
                "payload": {"cancellable": prior["cancellable"]},
            }
        context = {
            **session._context(),
            "contact_text": controller.gateway.vault.tokenize(row["text"]),
        }
        had_guard = client is not None and hasattr(client, "context_guard")
        original_guard = getattr(client, "context_guard", None)
        if client is not None:
            client.context_guard = controller.gateway.vault.ensure_safe
        try:
            response = session.receive(row["text"])
        finally:
            if client is not None:
                if had_guard:
                    client.context_guard = original_guard
                else:
                    del client.context_guard
        prediction = {
            "intent": response["intent"],
            "needs_clarification": response["needs_clarification"],
        }
        parameters = extract_slots(row["text"], context, intent=response["intent"])["parameters"]
        labels = expected.get("parameters")
        if not isinstance(labels, dict) or set(labels) != set(fields):
            raise ValueError("Frozen recipient, amount, date and product labels are required")
        results.append({
            "id": row["id"],
            "intent_correct": prediction["intent"] == expected["intent"]
            and SCENARIOS[prediction["intent"]] == expected["scenario"],
            "clarification_correct": prediction["needs_clarification"]
            == expected["needs_clarification"],
            "actual": prediction,
            "session_status": response["status"],
            "parameters": parameters,
            "expected_parameters": labels,
            "parameter_checks": {field: parameters[field] == labels[field] for field in fields},
        })
    metrics = {}
    for field in fields:
        correct = sum(row["parameter_checks"][field] for row in results)
        present = [row for row in results if row["expected_parameters"][field] is not None]
        present_correct = sum(row["parameter_checks"][field] for row in present)
        metrics[field] = {
            "correct": correct,
            "total": len(results),
            "accuracy": f"{correct}/{len(results)}" if results else None,
            "present_correct": present_correct,
            "present_total": len(present),
            "present_accuracy": f"{present_correct}/{len(present)}" if present else None,
            "present_status": "measured" if present else "not_evaluable",
        }
    exact = sum(all(row["parameter_checks"].values()) for row in results)
    return {
        "total": len(results),
        "intent_correct": sum(row["intent_correct"] for row in results),
        "clarification_correct": sum(row["clarification_correct"] for row in results),
        "planner_mode": client.mode if client else "scripted",
        "model": client.model if client else "scripted-bank-v1",
        "results": results,
        "parameter_accuracy": metrics,
        "parameter_exact_match": {
            "correct": exact,
            "total": len(results),
            "accuracy": f"{exact}/{len(results)}" if results else None,
        },
        "parameter_extractor": "deterministic_unsigned_candidates",
        "reference_clock": reference.state.now,
        "label_source_sha256": digest,
        "trusted_slot_context_hash": sha256(jcs_bytes(trusted_context)).hexdigest(),
        "classifier": "deployed_session_effective_intent_and_clarification",
        "scope": "fresh_session_classification_and_unsigned_parameters; downstream previews "
        "use the deterministic controller; scripted validates the pipeline, "
        "not real-model quality; null labels count as correct abstention",
    }
