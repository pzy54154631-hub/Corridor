"""Policy-preserving AgentDojo adapter backed by the unchanged native gateway.

Literal resolution is a weaker simulation of typed references. Native schedule
updates cancel and replace a signed SPA; recurring schedules are bounded to the
existing 365-day policy. User-profile writes have no native schema and reject.
"""

import re
from fractions import Fraction
from typing import Any

from corridor.bench.agentdojo_contract import (
    APPROVAL_MODES,
    digest,
    prompt_bound_approval,
    trusted_seed,
)
from corridor.bench.bank_cases import BankCase
from corridor.bench.investment_runner import _with_mfa
from corridor.bench.planning_runner import _cancel_spa, prepare_spa
from corridor.crypto.jcs import H
from corridor.schemas.bank import InputTerm, StateTerm
from corridor.schemas.state import BankState
from corridor.schemas.time import parse_time
from corridor.sim.device import Device
from corridor.sim.fixtures import make_state
from corridor.sim.keys import stable_id
from corridor.stores.bank import add_seconds, available

READ_TOOLS = frozenset({
    "get_iban",
    "get_balance",
    "get_most_recent_transactions",
    "get_scheduled_transactions",
    "read_file",
    "get_user_info",
})

WRITE_MAPPING = {
    "send_money": "native_transfer_signed_approval_mfa_dispatch",
    "schedule_transaction": "native_signed_spa_creation_365_day_bound",
    "update_scheduled_transaction": "native_cancel_then_replace_signed_spa",
    "update_password": "native_red_device_control_credential_version",
    "update_user_info": "native_policy_unsupported_no_user_profile_action_schema",
}


class NativeTransferGate:
    def __init__(
        self, prompt: str, environment: dict[str, Any], *, approval_mode: str = "prompt-bound"
    ) -> None:
        if approval_mode not in APPROVAL_MODES:
            raise ValueError("Unknown approval mode")
        self.prompt, self.approval_mode = prompt, approval_mode
        self.seed = trusted_seed(environment)
        self.step = 0
        self.mirror_divergence = False
        self.events: list[dict[str, Any]] = []
        state = make_state().model_dump(mode="json")
        state["now"] = self.seed["clock"]
        state["accounts"]["A_CURRENT"]["opening_balance"] = self.seed["balance"]
        # Pre-run environment and literal user IBANs are the only recipient sources.
        recipients = set(self.seed["recipients"]) | set(
            re.findall(r"\b[A-Z]{2}\d{10,32}\b", prompt)
        )
        self.refs = {recipient: f"AD_{digest(recipient)[:16]}" for recipient in recipients}
        state["contacts"] = {
            ref: {
                "id": ref,
                "owner": "C_DEMO",
                "name_token": f"NAME_{ref}",
                "phone_token": f"PHONE_{ref}",
                "destination_ref": f"DEST_{ref}",
                "verified_at": self.seed["clock"],
                "verification_ref": f"VERIFY_{ref}",
                "is_new": recipient not in self.seed["recipients"],
            }
            for recipient, ref in self.refs.items()
        }
        state["transactions"] = {}
        self.scheduled = {
            int(item["id"]): dict(item)
            for item in environment["bank_account"]["scheduled_transactions"]
        }
        self.schedule_refs: dict[int, str] = {}
        self.next_external_id = 1 + max(
            (
                int(item.get("id", 0))
                for item in environment["bank_account"]["transactions"]
                + environment["bank_account"]["scheduled_transactions"]
            ),
            default=0,
        )
        # Trusted pre-run schedules are cancellation-only imported authorities.
        # No creation event is fabricated, so they cannot execute as native SPA runs.
        for external_id, item in self.scheduled.items():
            reference = stable_id("E4-pre-run", external_id, "spa")
            start = f"{item['date']}T00:00:00Z"
            recurring = item.get("recurring", False)
            spa = Device().sign_spa({
                "id": reference,
                "source_ref": "A_CURRENT",
                "target": {"kind": "transfer", "contact_ref": self.refs[item["recipient"]]},
                "destination_version": 1,
                "amount": str(Fraction(str(item["amount"]))),
                "schedule": {"type": "monthly", "day": int(item["date"][-2:])}
                if recurring
                else {"type": "once"},
                "start": start,
                "end": add_seconds(start, 86400 * 365),
                "max_runs": 12 if recurring else 1,
                "creation_tier": "red"
                if recurring or Fraction(str(item["amount"])) > 1000
                else "yellow",
                "plan_id": stable_id(reference, 0, "plan"),
                "node_id": stable_id(reference, 0, "node"),
            })
            state["authorizations"][reference] = spa.model_dump(mode="json")
            run_id = stable_id(reference, 0, "imported-run")
            state["schedules"][run_id] = {
                "id": run_id,
                "version": 1,
                "owner": "C_DEMO",
                "plan_id": spa.plan_id,
                "node_id": spa.node_id,
                "spa_ref": reference,
                "run_index": 0,
                "action_template": {"kind": "transfer"},
                "input_refs": [],
                "due_at": start,
                "expires_at": add_seconds(start, 86400),
                "commitment_id": H({"spa_id": reference, "run_index": 0}),
                "status": "pending",
            }
            self.schedule_refs[external_id] = reference
        self.case = BankCase(
            BankState.model_validate(state), task_id="E4", subcase_id=digest(prompt)[:16]
        )

    def evaluate(self, function: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.step += 1
        result: dict[str, Any] = {
            "allow": False,
            "function": function,
            "argument_hash": digest(arguments),
            "reason": "unsupported_mapping",
            "mfa": False,
            "mirror_only_rejection": False,
            "receipt_id": None,
            "native_kinds": [],
        }
        if function in READ_TOOLS:
            result.update(allow=True, reason="read_only_external_tool")
        elif self.mirror_divergence:
            result.update(reason="mirror_divergence", mirror_divergence=True)
        elif function == "send_money":
            result.update(self._transfer(arguments))
            if result["allow"]:
                self.next_external_id += 1
        elif function == "schedule_transaction":
            result.update(self._schedule(arguments))
        elif function == "update_scheduled_transaction":
            result.update(self._update_schedule(arguments))
        elif function == "update_password":
            result.update(self._password(arguments))
        elif function == "update_user_info":
            result.update(
                reason="native_policy_unsupported",
                rule_ids=["TOOL-UNAVAILABLE"],
                policy_reason="No user-profile mutation exists in the native action schema",
            )
        balance = available(self.case.state, "A_CURRENT")
        result["mirror_balance"] = f"{balance.numerator}/{balance.denominator}"
        self.events.append(result)
        return result

    def record_external_result(self, decision: dict[str, Any], error: str | None) -> None:
        """An upstream failure never becomes a claimed successful native operation.

        Native settled effects are retained as evidence, never silently rolled
        back; the mirror cannot authorize another write after divergence.
        """
        if error and decision["function"] not in READ_TOOLS:
            self.mirror_divergence = True
            decision.update(
                allow=False,
                reason="upstream_execution_error",
                mirror_divergence=True,
                upstream_error_hash=digest(error),
            )

    def _money_source(self, arguments: dict[str, Any]) -> dict[str, Any] | None:
        try:
            amount = Fraction(str(arguments["amount"]))
        except (ValueError, KeyError, ZeroDivisionError):
            return {"allow": False, "reason": "invalid_amount"}
        if amount <= 0 or (amount * 100).denominator != 1:
            return {"allow": False, "reason": "invalid_amount"}
        recipient = str(arguments.get("recipient", ""))
        if recipient not in self.refs or not prompt_bound_approval(
            self.prompt, recipient, amount, self.seed
        ):
            return {"allow": False, "reason": "unresolved_source"}
        if self.approval_mode == "auto-deny":
            return {"allow": False, "reason": "approval_denied"}
        return None

    def _finish(self, prepared, *, control: bool = False) -> dict[str, Any]:
        if prepared.plan_rejection is not None and prepared.preparation_role == "setup":
            return {
                "allow": False,
                "evaluability": "not_evaluable",
                "reason": "unsupported_action_target_mapping",
                "rule_ids": prepared.plan_rejection.rule_ids,
            }
        verdict = prepared.proposal.verdict
        if verdict.outcome == "Reject":
            rules = list(verdict.rule_ids)
            return {
                "allow": False,
                "reason": "native_plan_reject"
                if prepared.plan_rejection is not None
                else "native_kernel_reject",
                "rule_ids": rules,
                "mirror_only_rejection": bool(rules) and all(r.startswith("BAL-") for r in rules),
            }
        mfa = verdict.tier == "red"
        if mfa:
            prepared = _with_mfa(self.case, prepared)
        if control:
            token = self.case.device.approve(
                prepared.action, prepared.cert, prepared.proposal.card, now=self.case.state.now
            )
            outcome = self.case.gateway.device_control(prepared.action, prepared.cert, token)
        else:
            outcome = self.case.dispatch(prepared)
        return {
            "allow": outcome.status in {"settled", "controlled"},
            "reason": outcome.status,
            "rule_ids": outcome.rule_ids,
            "mfa": mfa,
            "native_kinds": [prepared.action.kind],
            "receipt_id": outcome.receipt.id if outcome.receipt else None,
            "controlled_event_id": self.case.state.ledger[-1].id
            if outcome.status == "controlled"
            else None,
        }

    def _schedule(
        self, arguments: dict[str, Any], *, external_id: int | None = None
    ) -> dict[str, Any]:
        denied = self._money_source(arguments)
        if denied:
            return denied
        date = str(arguments.get("date", ""))
        try:
            start = f"{date}T00:00:00Z"
            parse_time(start)
        except ValueError:
            return {"allow": False, "reason": "invalid_schedule_date"}
        frozen_dates = {str(item["date"]) for item in self.scheduled.values()}
        if date not in frozen_dates and date not in self.prompt:
            return {"allow": False, "reason": "unresolved_schedule_source"}
        recurring = arguments.get("recurring")
        if type(recurring) is not bool:
            return {"allow": False, "reason": "invalid_schedule_recurrence"}
        if recurring and not (
            any(item.get("recurring") for item in self.scheduled.values())
            or re.search(r"\b(monthly|recurring|every month)\b", self.prompt, re.I)
        ):
            return {"allow": False, "reason": "unresolved_schedule_source"}
        amount = Fraction(str(arguments["amount"]))
        prepared, spa = prepare_spa(
            self.case,
            amount=f"{amount.numerator}/{amount.denominator}",
            start=start,
            end=add_seconds(start, 86400 * 365 if recurring else 86400),
            monthly_day=int(date[-2:]) if recurring else None,
            max_runs=12 if recurring else 1,
            target={"kind": "transfer", "contact_ref": self.refs[str(arguments["recipient"])]},
            step=self.step,
        )
        result = self._finish(prepared)
        result["recurrence_bound_days"] = 365 if recurring else 1
        if result["allow"]:
            reference = self.next_external_id if external_id is None else external_id
            self.scheduled[reference] = {**arguments, "id": reference}
            self.schedule_refs[reference] = spa.id
            if external_id is None:
                self.next_external_id += 1
        return result

    def _update_schedule(self, arguments: dict[str, Any]) -> dict[str, Any]:
        external_id = arguments.get("id")
        if type(external_id) is not int or external_id not in self.scheduled:
            return {"allow": False, "reason": "unresolved_schedule_reference"}
        # Match the pinned upstream tool's truthy optional-argument semantics.
        merged = {
            **self.scheduled[external_id],
            **{key: value for key, value in arguments.items() if key != "id" and value},
        }
        denied = self._money_source(merged)
        if denied:
            return denied
        # Validate the replacement before cancelling anything. A separate isolated
        # gateway has no effects on this episode and uses exactly the same rules.
        probe = self.__class__.__new__(self.__class__)
        probe.__dict__ = {
            **self.__dict__,
            "scheduled": dict(self.scheduled),
            "schedule_refs": dict(self.schedule_refs),
        }
        probe.case = BankCase(self.case.state, task_id="E4-check", subcase_id=digest(merged)[:16])
        checked = probe._schedule(merged, external_id=external_id)
        if not checked["allow"]:
            return checked
        spa = self.case.state.authorizations[self.schedule_refs[external_id]]
        cancelled = _cancel_spa(self.case, spa, step=self.step)
        if cancelled.status != "controlled":
            return {"allow": False, "reason": cancelled.status, "rule_ids": cancelled.rule_ids}
        self.step += 1
        result = self._schedule(merged, external_id=external_id)
        result["native_kinds"] = ["cancel_schedule", *result.get("native_kinds", [])]
        result["cancel_receipt_id"] = cancelled.receipt.id if cancelled.receipt else None
        result["partial_native_effect"] = not result["allow"]
        return result

    def _password(self, arguments: dict[str, Any]) -> dict[str, Any]:
        password = arguments.get("password")
        if not isinstance(password, str) or not password or password not in self.prompt:
            return {"allow": False, "reason": "unresolved_source"}
        if self.approval_mode == "auto-deny":
            return {"allow": False, "reason": "approval_denied"}
        case = self.case
        item = case.device.confirm_input(
            intent_id=case.intent(self.step),
            slot="password_digest",
            value=digest(password),
            now=case.state.now,
        )
        authority = case.operation("password_change_request", ["C_DEMO"], [item], step=self.step)
        prepared = case.prepare(
            "password_change_request",
            {"principal": "C_DEMO"},
            {
                "principal": StateTerm(
                    collection="customers",
                    object_ref="C_DEMO",
                    field="id",
                    version=case.state.customers["C_DEMO"].version,
                ),
            },
            inputs=[item],
            authorization=authority,
            step=self.step,
        )
        return self._finish(prepared, control=True)

    def _transfer(self, arguments: dict[str, Any]) -> dict[str, Any]:
        denied = self._money_source(arguments)
        if denied:
            return denied
        recipient = str(arguments.get("recipient", ""))
        try:
            amount = Fraction(str(arguments["amount"]))
        except (ValueError, KeyError, ZeroDivisionError):
            return {"allow": False, "reason": "invalid_amount"}
        if amount <= 0 or (amount * 100).denominator != 1:
            return {"allow": False, "reason": "invalid_amount"}
        resolved = recipient in self.refs and prompt_bound_approval(
            self.prompt, recipient, amount, self.seed
        )
        if not resolved:
            return {"allow": False, "reason": "unresolved_source"}
        if self.approval_mode == "auto-deny":
            return {"allow": False, "reason": "approval_denied"}
        ref, case = self.refs[recipient], self.case
        value = f"{amount.numerator}/{amount.denominator}"
        item = case.device.confirm_input(
            intent_id=case.intent(self.step),
            slot="amount",
            value={"amount": value, "ccy": "CNY"},
            now=case.state.now,
        )
        authority = case.operation(
            "transfer", ["A_CURRENT", ref], [item], amount_bound=value, step=self.step
        )
        prepared = case.prepare(
            "transfer",
            {"source_ref": "A_CURRENT", "contact_ref": ref, "amount_ref": item.id},
            {
                "source_ref": StateTerm(
                    collection="accounts",
                    object_ref="A_CURRENT",
                    field="id",
                    version=case.state.accounts["A_CURRENT"].version,
                ),
                "contact_ref": StateTerm(
                    collection="contacts",
                    object_ref=ref,
                    field="id",
                    version=case.state.contacts[ref].version,
                ),
                "amount_ref": InputTerm(input_ref=item.id, slot="amount"),
            },
            inputs=[item],
            authorization=authority,
            step=self.step,
        )
        return self._finish(prepared)
