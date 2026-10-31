"""Isolated domestic write experiments; production never imports this module.

Checks are selected before verdict construction and only effective decisions
advance private state. A5 always uses the unmodified production gateway.
"""

import ast
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from hashlib import sha256
from importlib import import_module
from pathlib import Path
from typing import Any, cast

from corridor.bench.bank_cases import BankCase, PreparedCase
from corridor.bench.baseline_exec import BaselineExecutor, BaselineResult
from corridor.bench.investment_runner import _with_mfa
from corridor.bench.users import SimulatedUser, signed_goal
from corridor.bench.write_effects import LOCAL_WRITE_KINDS, execute_local_write, local_effects
from corridor.crypto.jcs import H
from corridor.gateway.bank_adapter import compile_order, movements, receipt_updates
from corridor.gateway.mfa import MFAVerifier
from corridor.gateway.plans import GREEN_TOOLS, SUPPORTED_TOOLS, validate_plan
from corridor.gateway.service import GatewayResult, Proposal
from corridor.kernel.binding import (
    binding,
    dependency_hashes,
    intent_key,
    mfa_scope,
    resolve_amount,
)
from corridor.kernel.rules import RULES
from corridor.schemas.bank import (
    ApprovalToken,
    BusinessEvent,
    Handle,
    OperationAuthorization,
    Plan,
    ReadTerm,
    ScheduledPaymentAuthorization,
)
from corridor.schemas.base import loads_exact
from corridor.sim.keys import signing_key, stable_id
from corridor.stores.bank import latest_events

LAYERS = ("L-auth", "L-prov", "L-dom", "L-out")
COMMON = "common"
CONFIGS: dict[str, frozenset[str]] = {
    "A0": frozenset(),
    "A1": frozenset(),
    "A2": frozenset({"L-auth"}),
    "A3": frozenset({"L-prov"}),
    "A4": frozenset({"L-auth", "L-prov"}),
    "A5": frozenset(LAYERS),
    **{f"A5-minus-{layer[2:]}": frozenset(set(LAYERS) - {layer}) for layer in LAYERS},
}
ROOT = Path(__file__).resolve().parents[3]
MANIFEST = ROOT / "bench" / "ablation_manifest.json"
_kernel = import_module("corridor.kernel.check")
_spa = import_module("corridor.kernel.spa")
BANK_WRITE_KINDS = frozenset({
    "transfer",
    "cancel_subscription",
    "purchase_product",
    "lock_funds",
    "unlock_funds",
    "freeze_card",
    "unfreeze_card",
    "report_card_lost",
    "restore_card",
    "lower_card_limit",
    "raise_card_limit",
    "restrict_card",
    "relax_card_restriction",
})


def configure_experiment(case: BankCase, config: str) -> None:
    """Configure a newly created bench case before its first proposal.

    Only this explicitly isolated case gets a pure proposal preview. This keeps
    disabled production checks from changing its security before the selected
    executor runs. A5 retains the production lifecycle unchanged.
    """
    if config == "A5":
        return
    if case.preparations:
        raise ValueError("Configure an experiment before preparing any proposal")
    if config not in {*CONFIGS, "B_prompt", "B_guard"}:
        raise ValueError("Unknown experimental configuration")
    cache: dict[str, Any] = {}
    setattr(case, "_domestic_experiment_executors", cache)
    setattr(case, "_domestic_preparation_config", config)
    if config not in {"A0", "A1", "B_prompt", "B_guard"}:
        cache[config] = TransferExperiment(case, config)

    def preview(
        action: Any, cert: Any, request_id: Any = None, injection_flag: bool = False
    ) -> Proposal:
        # A diagnostic preview does not become an execution decision. In
        # particular, it cannot write audit/lockout/approval/nonce bookkeeping.
        verdict = _kernel.check(action, cert, case.state, phase="propose")
        card = verdict.verified_facts.get("card_core", {})
        return Proposal(verdict, card if isinstance(card, dict) else {}, H(action), H(cert))

    setattr(case.gateway, "propose", preview)

    def register(plan: Plan) -> GatewayResult:
        return _register_selected_plan(case, plan, CONFIGS.get(config, frozenset()))

    setattr(case.gateway, "submit_plan", register)


def _register_selected_plan(case: BankCase, plan: Plan, active: frozenset[str]) -> GatewayResult:
    """Select registration checks before touching isolated experiment state."""
    gateway, state = case.gateway, case.state
    errors = SelectedErrors(active)
    if "L-auth" in active:
        if plan.principal in state.security.locked_at and any(
            node.tool not in GREEN_TOOLS - {"risk_assess"} for node in plan.nodes
        ):
            errors.append("APR-LOCK")
        if plan.plan_id in gateway._blocked_plans:
            errors.append("APR-VOID")
    if "L-prov" in active:
        try:
            gateway.vault.ensure_safe(plan)
            if (
                plan.principal not in state.customers
                or state.security.active_device_sessions.get(plan.principal)
                != plan.device_session_id
            ):
                errors.append("PLAN-OWNER")
            rule = validate_plan(plan, state, supported_tools=SUPPORTED_TOOLS)
            if rule:
                errors.append(rule)
            for node in plan.nodes:
                if node.tool == "sandbox_analyze":
                    for field_name, term in node.args.items():
                        ref = (
                            term.value
                            if isinstance(term, ReadTerm)
                            else (term.ref if isinstance(term, Handle) else None)
                        )
                        if not gateway._sandbox.has_handle(plan.principal, field_name, ref):
                            errors.append("PLAN-SCOPE")
            previous = gateway._plans.get(plan.plan_id)
            if previous is None and plan.ownership_epoch != 0:
                errors.append("PLAN-OWNER")
            if previous is not None and previous != plan:
                errors.append("PLAN-SCOPE")
        except (ValueError, TypeError, AttributeError) as exc:
            errors.append("PII-CTX" if str(exc) == "PII-CTX" else "PLAN-SCHEMA")
    if not gateway.audit.verify():
        errors.append("AUD-CHAIN")
    if errors:
        return GatewayResult("rejected", list(dict.fromkeys(errors)))
    try:
        gateway.audit.append(
            "plan_registered",
            now=state.now,
            principal_token=plan.principal,
            plan_id=plan.plan_id,
            payload={"plan": plan.model_dump(mode="json")},
        )
    except OSError:
        return GatewayResult("rejected", ["AUD-WRITE"])
    gateway._plans[plan.plan_id] = plan
    return GatewayResult("registered")


def rule_layer(rule: str) -> str:
    if rule.startswith(("AUD-", "SBX-")) or rule == "BANK-RECEIPT":
        return COMMON
    if rule in {"RCPT-MISMATCH", "PII-OUT"}:
        return "L-out"
    if rule.startswith(("TIER-", "AUTH-", "MFA-", "APR-", "BRK-")) or rule in {
        "CTRL-MFA",
        "DOMAIN-SPA-TIER",
        "READ-ROLE",
    }:
        return "L-auth"
    if rule.startswith(("PROV-", "SCHEMA-", "PLAN-")) or rule in {
        "CERT-HASH",
        "TOOL-UNAVAILABLE",
        "PII-CTX",
    }:
        return "L-prov"
    if rule.startswith(("BAL-", "DOMAIN-", "IDEM-", "BIC-", "ALLOC-", "RISK-")):
        return "L-dom"
    raise ValueError(f"Unmapped rule: {rule}")


def check_inventory() -> list[dict[str, Any]]:
    """Inventory every registry rule, gateway rule site and branch expression.

    Branches that merely orchestrate multiple checks belong to infrastructure;
    their actual rejection rule sites remain individually mapped. Source hashes
    make a changed check fail validation even if its line number is unchanged.
    """
    result: list[dict[str, Any]] = [
        {"id": f"rule:{rule}", "kind": "rule", "layer": rule_layer(rule)} for rule in RULES
    ]
    for path in sorted((ROOT / "src/corridor/gateway").glob("*.py")):
        relative = path.relative_to(ROOT).as_posix()
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        parents = {
            child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)
        }
        for node in ast.walk(tree):
            if not isinstance(node, (ast.If, ast.IfExp, ast.Assert, ast.Constant)):
                continue
            ancestor = parents.get(node)
            while ancestor is not None and not isinstance(
                ancestor, (ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                ancestor = parents.get(ancestor)
            function = ancestor.name if ancestor is not None else "module"
            if isinstance(node, ast.Constant):
                if not isinstance(node.value, str) or node.value not in RULES:
                    continue
                category, layer = f"rule-site:{node.value}", rule_layer(node.value)
            else:
                category = type(node).__name__
                # Pure output and privacy helpers are defense checks; mixed
                # dispatch orchestration is shared plumbing, not a fifth layer.
                if path.stem in {"facts", "output_guard"}:
                    layer = "L-out"
                elif path.stem in {"plans", "handles", "preview", "privacy"}:
                    layer = "L-prov"
                elif path.stem in {"mfa", "breaker"} or function in {
                    "_token_rule",
                    "_grant_rule",
                    "_approval_request",
                    "approval_budget",
                    "_record_verdict",
                    "_safety_signal",
                    "_mfa_observed",
                    "unlock",
                    "_request_unlock_mfa",
                    "_request_mfa",
                    "submit_mfa",
                    "login",
                }:
                    layer = "L-auth"
                elif path.stem in {"investments", "allocations"}:
                    layer = "L-dom"
                else:
                    layer = COMMON
            expression = ast.dump(
                node.test if hasattr(node, "test") else node, include_attributes=False
            )
            result.append({
                "id": f"gateway:{relative}:{function}:{node.lineno}:{node.col_offset}:{category}",
                "kind": category,
                "layer": layer,
                "source": relative,
                "line": node.lineno,
                "check_hash": sha256(expression.encode("utf-8")).hexdigest(),
            })
    return result


def validate_manifest(path: Path = MANIFEST) -> list[str]:
    if not path.exists():
        return ["manifest_missing"]
    body = loads_exact(path.read_bytes())
    checks = body.get("checks", [])
    ids = [item.get("id") for item in checks]
    errors = ["duplicate_mapping"] if len(ids) != len(set(ids)) else []
    if any(item.get("layer") not in {*LAYERS, COMMON} for item in checks):
        errors.append("unknown_layer")
    expected = {item["id"]: item for item in check_inventory()}
    actual = {item.get("id"): item for item in checks}
    if expected.keys() != actual.keys():
        errors.append("unmapped_or_stale_checks")
    if any(actual.get(key) != value for key, value in expected.items()):
        errors.append("changed_check_or_mapping")
    return errors


class SelectedErrors(list[str]):
    """A selected check sink; disabled checks cannot influence a verdict."""

    def __init__(self, active: frozenset[str]) -> None:
        super().__init__()
        self.active = active
        self.observed: list[str] = []

    def append(self, rule: str) -> None:
        self.observed.append(rule)
        if rule_layer(rule) in self.active or rule_layer(rule) == COMMON:
            super().append(rule)

    def extend(self, rules: Iterable[str]) -> None:
        for rule in rules:
            self.append(rule)


@dataclass
class ExperimentalSecurity:
    rejected_proposals: int = 0
    injection_hits: int = 0
    consumed_nonces: list[str] = field(default_factory=list)
    approval_consumed: int = 0
    consumed_grants: list[str] = field(default_factory=list)
    locked: bool = False
    bank_unknown: bool = False


class TransferExperiment:
    """Bench-only write executor with privately owned mutable state.

    Only final effective decisions advance the experiment's lockout/nonce state.
    Rejects increment the effective reject counter; Escalate does not. There is
    no claim of complete production lockout timing equivalence for partial rows.
    """

    def __init__(self, case: BankCase, config: str) -> None:
        if config not in {*CONFIGS, "B_prompt", "B_guard"} or config == "A1":
            raise ValueError("Expected a layered transfer configuration")
        self.case, self.config, self.active = case, config, CONFIGS.get(config, frozenset())
        self.state = case.state.model_copy(deep=True)
        self.backend = BaselineExecutor(self.state, "d0")
        self.mfa = MFAVerifier(signing_key("mfa"))
        self.security = ExperimentalSecurity(
            consumed_nonces=list(self.state.security.consumed_nonces),
            consumed_grants=list(self.state.security.consumed_grant_ids),
            locked=case.device.principal in self.state.security.locked_at,
        )
        self.actions: list[dict[str, Any]] = []
        self.approvals: list[dict[str, Any]] = []
        self.mfa_attempted = False
        self.confirmation_requested = False

    def _result(
        self, status: str, errors: SelectedErrors, *, receipt: Any = None, reason: str | None = None
    ) -> dict[str, Any]:
        final = self.backend.bank.final_state()
        final["schedules"] = {
            key: value.model_dump(mode="json") for key, value in self.state.schedules.items()
        }
        effects = [*final["events"], *local_effects(self.state)]
        return {
            "status": status,
            "config": self.config,
            "mode": "effective-write-subset",
            "scope": "registered single-node bank writes; no allocation or lifecycle equivalence",
            "reason": reason,
            "rule_ids": list(dict.fromkeys(errors)),
            "observed_rule_ids": list(dict.fromkeys(errors.observed)),
            "layer_status": {
                layer: {
                    "status": "evaluated"
                    if layer in self.active and status != "not_evaluable" and layer != "L-out"
                    else "not_evaluable",
                    "reason": "no final-display observation in write execution"
                    if layer == "L-out"
                    else "enabled applicable checks"
                    if layer in self.active
                    else "disabled by configuration",
                }
                for layer in LAYERS
            },
            "receipt": receipt.model_dump(mode="json") if receipt else None,
            "initial_state": self.case.initial.model_dump(mode="json"),
            "final_state": final,
            "bank_effects": effects,
            "gateway_ledger": [
                item.model_dump(mode="json")
                for item in self.state.ledger
                if item.kind in LOCAL_WRITE_KINDS
            ],
            "actual_actions": list(self.actions),
            "approvals": list(self.approvals),
            "experiment_state": asdict(self.security),
            "confirmation_requested": self.confirmation_requested,
            "mfa_attempted": self.mfa_attempted,
            "bank_unknown": self.security.bank_unknown,
        }

    def execute(
        self,
        prepared: PreparedCase,
        *,
        approve: bool = True,
        allow_mfa: bool = True,
        token: ApprovalToken | None = None,
    ) -> dict[str, Any]:
        errors = SelectedErrors(self.active)
        self.mfa_attempted = False
        self.confirmation_requested = False
        if prepared.plan_rejection is not None:
            errors.extend(prepared.plan_rejection.rule_ids)
            if errors:
                self.security.rejected_proposals += 1
                if "L-auth" in self.active and self.security.rejected_proposals >= 3:
                    self.security.locked = True
                return self._result("rejected", errors, reason="plan_registration_rejected")
            return self._result(
                "not_evaluable",
                errors,
                reason="plan_rejection_requires_effective_preparation",
            )
        if self.security.bank_unknown:
            return self._result("unknown", errors, reason="prior unknown bank result; safe stop")
        action, cert = prepared.action, prepared.cert
        current = self.case.state
        # Only explicitly signed fixture additions enter the isolated snapshot.
        # Experimental ledger and security never flow back into the source case.
        self.state = self.state.model_copy(
            update={
                "authorizations": current.authorizations,
                "user_inputs": current.user_inputs,
                "security": self.state.security.model_copy(
                    update={
                        "consumed_nonces": list(self.security.consumed_nonces),
                        "consumed_grant_ids": list(self.security.consumed_grants),
                    }
                ),
            },
            deep=True,
        )
        state = self.state
        auth = state.authorizations.get(cert.authorization_ref)
        if (
            action.kind not in BANK_WRITE_KINDS | LOCAL_WRITE_KINDS
            or action.action_id in state.schedules
            or len(prepared.plan.nodes) != 1
            or prepared.plan.nodes[0].depends_on
            or action.args.model_dump().get("allocation_ref")
        ):
            return self._result(
                "not_evaluable", errors, reason="outside calibrated single-node write subset"
            )
        try:
            facts = binding(action, cert, state)
            amount = resolve_amount(action, state)
        except (ValueError, KeyError, AttributeError, TypeError):
            return self._result(
                "not_evaluable", errors, reason="typed references cannot be resolved"
            )
        # Pure family calls have no mutation. Selection is fixed before any call;
        # errors from disabled checks never enter the effective error collection.
        if cert.action_hash != H(action) or cert.dependency_hashes != dependency_hashes(
            action, cert, state
        ):
            errors.append("CERT-HASH")
        if any(event.ts > state.now for event in state.ledger):
            errors.append("SCHEMA-TIME")
        if action.kind not in state.policy.tool_whitelist:
            errors.append("TOOL-UNAVAILABLE")
        _kernel._authority(action, cert, state, errors)
        _kernel._card_origin(action, cert, state, errors)
        _kernel._provenance(action, cert, state, errors)
        _kernel._domain(action, cert, state, amount, errors)
        errors.extend(_kernel.investment_errors(action, cert, state))
        errors.extend(_kernel.allocation_errors(action, cert, state))
        errors.extend(_kernel.compensation_errors(action, state, amount))
        errors.extend(_kernel.card_errors(action, state))
        spa = _kernel.inspect_spa(action, cert, state, amount)
        errors.extend(spa.errors)
        if action.kind == "schedule_transfer" and not isinstance(
            auth, ScheduledPaymentAuthorization
        ):
            errors.append("DOMAIN-SPA")
        if action.kind == "aa_request":
            args = action.args.model_dump(mode="python")
            if args["source_bill_ref"] != args["total_ref"]:
                errors.append("PROV-COHERENCE")
            if (
                amount is None
                or amount * 100 < len(args["participant_refs"])
                or any(
                    ref not in state.contacts
                    or state.contacts[ref].owner != action.principal
                    or not ref.isascii()
                    for ref in args["participant_refs"]
                )
            ):
                errors.append("DOMAIN-AA")
        # A malformed source term can return early in the production walker.
        # Retained authorization checks must still inspect the resolved action.
        if "L-auth" in self.active:
            args = action.args.model_dump(mode="python")
            if (
                isinstance(auth, ScheduledPaymentAuthorization)
                and action.kind == "schedule_transfer"
            ):
                # SPA has a distinct immutable scope checked by inspect_spa.
                # Scope failures stay authorization failures even with L-dom off.
                if not _spa._action_scope(action, auth, True) or amount != auth.amount:
                    errors.append("AUTH-SCOPE")
                for field_name, ref in args.items():
                    if field_name.endswith("_ref") and ref in state.user_inputs:
                        _kernel._input(ref, action, cert, state, errors)
            elif not isinstance(auth, OperationAuthorization):
                errors.append("AUTH-SCOPE")
            else:
                for field_name, collection_name in _kernel._COLLECTIONS.items():
                    if field_name not in args or args[field_name] is None:
                        continue
                    refs = (
                        args[field_name] if field_name == "participant_refs" else [args[field_name]]
                    )
                    collection = getattr(state, collection_name)
                    for ref in refs:
                        if ref not in auth.object_refs:
                            errors.append("AUTH-SCOPE")
                        obj = (
                            next((item for item in state.catalog.products if item.id == ref), None)
                            if collection_name == "catalog"
                            else collection.get(ref)
                        )
                        if (
                            obj is None
                            or getattr(obj, "owner", getattr(obj, "principal", action.principal))
                            != action.principal
                        ):
                            errors.append("AUTH-PRINCIPAL")
                for field_name, ref in args.items():
                    if field_name.endswith("_ref") and ref in state.user_inputs:
                        _kernel._input(ref, action, cert, state, errors)
        if "L-prov" in self.active:
            rule = self.case.gateway._plan_rule(action, cert, state)
            if rule:
                errors.append(rule)
        # Authorization-invalid plans remain a separate check even if provenance
        # is disabled. The production plan function can return early on provenance.
        if (
            action.plan_id in self.case.gateway._voided_plans
            or action.plan_id in self.case.gateway._blocked_plans
        ):
            errors.append("APR-VOID")
        if self.security.locked:
            errors.append("APR-LOCK")
        has_mfa = _kernel._valid_mfa(action, cert, state, facts, errors)
        # Missing MFA is checked independently, before effective verdict creation.
        missing_mfa = facts.tier == "red" and not has_mfa and "L-auth" in self.active
        if missing_mfa and approve and allow_mfa and not errors:
            self.mfa_attempted = True
            challenge = self.mfa.challenge(
                principal=action.principal,
                device_session_id=action.device_session_id,
                scope_hash=mfa_scope(action, H(facts.card_core)),
                now=state.now,
                sequence=len(self.actions),
            )
            grant = self.case.device.complete_mfa(self.mfa, challenge, now=state.now)
            if grant is None:
                return self._result("waiting_mfa", errors, reason="simulated MFA failed")
            cert = cert.model_copy(update={"mfa_grant_refs": [grant.grant_id]})
            grants = {**state.security.mfa_grants, grant.grant_id: grant}
            state = state.model_copy(
                update={"security": state.security.model_copy(update={"mfa_grants": grants})},
                deep=True,
            )
            self.state = state
            has_mfa = _kernel._valid_mfa(action, cert, state, facts, errors)
            missing_mfa = not has_mfa
        if missing_mfa:
            errors.append("TIER-MFA-REQUIRED")
        if "L-auth" in self.active and not missing_mfa and not errors:
            self.confirmation_requested = True
            if not approve:
                errors.append("APR-REQUIRED")
            else:
                try:
                    token = token or self.case.device.approve(
                        action, cert, facts.card_core, now=state.now
                    )
                except ValueError:
                    errors.append("APR-SWAP")
                    return self._result("rejected", errors, reason="device refused invalid binding")
                # Register only the canonical experimental card on a shallow
                # gateway copy; no production cards or security are modified.
                from copy import copy

                checker = copy(self.case.gateway)
                checker._cards = {
                    **checker._cards,
                    checker._card_key(action, cert): facts.card_core,
                }
                rule = checker._token_rule(action, cert, token, state)
                if rule:
                    errors.append(rule)
                if token.card_hash != H(facts.card_core):
                    errors.append("APR-DRIFT")
                for ref in cert.mfa_grant_refs:
                    grant = state.security.mfa_grants.get(ref)
                    if grant is None:
                        errors.append("MFA-INVALID")
                    elif ref in self.security.consumed_grants:
                        errors.append("MFA-REUSE")
        key = intent_key(action, state)
        previous = latest_events(state).get(key)
        if previous is not None and previous.event != "failed":
            errors.append("IDEM-DUP")
        if not self.case.gateway.audit.verify():
            errors.append("AUD-CHAIN")
        if errors:
            rejects = [rule for rule in errors if RULES[rule].outcome == "Reject"]
            if rejects:
                self.security.rejected_proposals += 1
                if any(rule.startswith("PROV-") for rule in rejects):
                    self.security.injection_hits += 1
                if "L-auth" in self.active and (
                    self.security.rejected_proposals >= 3 or self.security.injection_hits >= 2
                ):
                    self.security.locked = True
            return self._result("rejected" if rejects else "waiting_mfa", errors)
        # Local schedule/AA writes have no BankOrder. Their ledger/receipt
        # builders are bench-only and run after the same effective decision.
        if action.kind in LOCAL_WRITE_KINDS:
            try:
                updated, analysis = execute_local_write(
                    action, cert, state, token, verified="L-auth" in self.active
                )
            except (ValueError, KeyError, TypeError, AttributeError):
                return self._result(
                    "not_evaluable", errors, reason="local write inputs cannot be resolved"
                )
            if "L-auth" in self.active and token is not None:
                self.security.consumed_nonces.append(token.nonce)
                self.security.approval_consumed += 1
                self.security.consumed_grants.extend(cert.mfa_grant_refs)
                self.approvals.append(token.model_dump(mode="json"))
            self.state = updated
            self.actions.append(action.model_dump(mode="json"))
            result = self._result(
                "controlled" if action.kind == "schedule_transfer" else "settled", errors
            )
            result["analysis_receipt"] = analysis.model_dump(mode="json") if analysis else None
            return result
        # Compilation is pure and must succeed before any authorization is spent.
        try:
            order = compile_order(action, cert, state)
        except (ValueError, KeyError, TypeError, AttributeError):
            return self._result("not_evaluable", errors, reason="order cannot be compiled")
        # No experiment state has changed before the final effective verdict.
        if "L-auth" in self.active and token is not None:
            self.security.consumed_nonces.append(token.nonce)
            self.security.approval_consumed += 1
            self.security.consumed_grants.extend(cert.mfa_grant_refs)
            self.approvals.append(token.model_dump(mode="json"))
        self.actions.append(order.model_dump(mode="json"))
        self.backend.bank.faults.update(self.case.gateway.bank.faults)
        try:
            result = self.backend.execute(
                order, SimulatedUser("all_approve", signed_goal({"kind": action.kind}))
            )
        except (TimeoutError, OSError):
            self.security.bank_unknown = True
            return self._result("unknown", errors, reason="bank response unknown")
        if result.receipt is not None and result.receipt.id not in state.receipts:
            debit, credit = movements(order, state)
            event = BusinessEvent(
                id=stable_id(key, len(state.ledger), "experiment-event"),
                intent_key=key,
                event="reserved" if result.receipt.status == "pending" else result.receipt.status,
                kind=action.kind,
                principal=action.principal,
                debit=debit,
                credit=credit,
                bank_receipt_id=result.receipt.receipt_id,
                object_delta={
                    "order": order.model_dump(mode="json"),
                    "action": action.model_dump(mode="json"),
                    "cert": cert.model_dump(mode="json"),
                },
                ts=state.now,
            )
            self.state = state.model_copy(
                update={
                    "ledger": [*state.ledger, event],
                    **receipt_updates(order, result.receipt, state),
                }
            )
        return self._result(result.status, errors, receipt=result.receipt)


def execute_prepared(
    config: str,
    case: BankCase,
    prepared: PreparedCase,
    *,
    approve: bool = True,
    allow_mfa: bool = True,
) -> dict[str, Any]:
    """Execute an independently prepared cell; inputs are not A5 success traces."""
    if config not in {*CONFIGS, "B_prompt", "B_guard"}:
        raise ValueError(f"Unknown configuration: {config}")
    cache = getattr(case, "_domestic_experiment_executors", None)
    if cache is None:
        cache = {}
        setattr(case, "_domestic_experiment_executors", cache)
    if prepared.plan_rejection is not None and (
        prepared.preparation_role == "setup" or config in {"A0", "A1", "A5", "B_prompt", "B_guard"}
    ):
        setup = prepared.preparation_role == "setup"
        evaluated = config == "A5" and not setup
        return {
            "status": "rejected" if evaluated else "not_evaluable",
            "evaluability": "evaluated" if evaluated else "not_evaluable",
            "reason": "unsupported_action_target_mapping"
            if setup
            else "plan_registration_rejected"
            if evaluated
            else "plan_rejection_requires_effective_preparation",
            "rejection_boundary": "plan_registration",
            "config": config,
            "rule_ids": prepared.plan_rejection.rule_ids,
            "receipt": None,
            "bank_effects": [],
            "initial_state": case.initial.model_dump(mode="json"),
            "final_state": case.gateway.bank.final_state(),
            "approvals": [],
            "actual_actions": [],
            "confirmation_requested": False,
            "mfa_attempted": False,
        }
    if config == "A1" and prepared.action.kind in LOCAL_WRITE_KINDS:
        return {
            "status": "not_evaluable",
            "reason": "frozen D3 static whitelist has no schedule/AA ledger semantics",
            "config": config,
            "receipt": None,
            "bank_effects": [],
            "initial_state": case.initial.model_dump(mode="json"),
            "final_state": case.gateway.bank.final_state(),
            "approvals": [],
            "actual_actions": [],
            "layer_status": {layer: {"status": "not_evaluable"} for layer in LAYERS},
        }
    if config not in {"A0", "A1", "A5", "B_prompt", "B_guard"} or (
        config != "A5" and prepared.action.kind in LOCAL_WRITE_KINDS
    ):
        if config not in cache:
            cache[config] = TransferExperiment(case, config)
        engine = cast(TransferExperiment, cache[config])
        partial_result = engine.execute(prepared, approve=approve, allow_mfa=allow_mfa)
        if getattr(case, "_domestic_preparation_config", None) == config:
            security = engine.state.security.model_copy(
                update={
                    "consumed_nonces": engine.security.consumed_nonces,
                    "consumed_grant_ids": engine.security.consumed_grants,
                    "reject_streak": {case.device.principal: engine.security.rejected_proposals}
                    if engine.security.rejected_proposals
                    else {},
                    "locked_at": {case.device.principal: engine.state.now}
                    if engine.security.locked
                    else {},
                }
            )
            engine.state = engine.state.model_copy(update={"security": security})
            case.gateway.store.update(
                **{
                    name: getattr(engine.state, name)
                    for name in (
                        "ledger",
                        "receipts",
                        "cards",
                        "contacts",
                        "subscriptions",
                        "positions",
                        "orders",
                        "schedules",
                    )
                },
                security=security,
            )
        return partial_result
    approvals: list[dict[str, Any]] = []
    actions: list[dict[str, Any]] = []
    result: Any
    mfa_attempted = False
    if config == "A5":
        if approve and allow_mfa and prepared.proposal.verdict.outcome == "Escalate":
            mfa_attempted = True
            prepared = _with_mfa(case, prepared)
        token = None
        if approve and prepared.proposal.verdict.tier != "green":
            try:
                token = case.device.approve(
                    prepared.action, prepared.cert, prepared.proposal.card, now=case.state.now
                )
                approvals.append(token.model_dump(mode="json"))
            except ValueError:
                # A device refusal cannot be converted into synthetic authority.
                token = None
        result = (
            case.gateway.query(prepared.action, prepared.cert)
            if prepared.proposal.verdict.tier == "green"
            else case.gateway.dispatch(prepared.action, prepared.cert, token)
        )
        final = case.gateway.bank.final_state()
        rules = result.rule_ids
        if result.receipt:
            actions.append(
                compile_order(prepared.action, prepared.cert, case.state).model_dump(mode="json")
            )
        elif prepared.action.kind in LOCAL_WRITE_KINDS and result.status in {
            "settled",
            "controlled",
        }:
            actions.append(prepared.action.model_dump(mode="json"))
    else:
        # D0/D3 share the same canonical typed-order compilation capability.
        # This is not a policy check: a wrong, resolvable recipient can execute.
        if config not in cache:
            cache[config] = BaselineExecutor(case.state, "d3" if config == "A1" else "d0")
        executor = cache[config]
        executor.bank.faults.update(case.gateway.bank.faults)
        try:
            order = compile_order(prepared.action, prepared.cert, case.state)
        except (ValueError, KeyError, TypeError, AttributeError):
            return {
                "status": "not_evaluable",
                "reason": "baseline has no order compiler for action",
                "config": config,
                "receipt": None,
                "rule_ids": [],
                "bank_effects": [],
                "initial_state": case.initial.model_dump(mode="json"),
                "final_state": executor.bank.final_state(),
                "approvals": [],
                "actual_actions": [],
                "layer_status": {layer: {"status": "not_evaluable"} for layer in LAYERS},
            }
        user = SimulatedUser(
            "all_approve" if approve else "careful", signed_goal({"kind": prepared.action.kind})
        )
        try:
            result = executor.execute(order, user)
        except (TimeoutError, OSError):
            result = BaselineResult("unknown", "bank_unknown")
        final = executor.bank.final_state()
        rules = [result.reason] if result.reason else []
        actions.append(order.model_dump(mode="json"))
        if getattr(case, "_domestic_preparation_config", None) == config:
            receipt = result.receipt or executor.bank.lookup(order.intent_key)
            if receipt is not None and receipt.id not in case.state.receipts:
                state = case.state
                debit, credit = movements(order, state)
                event = BusinessEvent(
                    id=stable_id(order.intent_key, len(state.ledger), "baseline-event"),
                    intent_key=order.intent_key,
                    event="unknown"
                    if result.status == "unknown"
                    else "reserved"
                    if receipt.status == "pending"
                    else receipt.status,
                    kind=order.kind,
                    principal=order.principal,
                    debit=debit,
                    credit=credit,
                    bank_receipt_id=receipt.id,
                    object_delta={"order": order.model_dump(mode="json")},
                    ts=state.now,
                )
                case.gateway.store.update(
                    ledger=[*state.ledger, event],
                    **receipt_updates(order, receipt, state),
                )
    final["schedules"] = {
        key: value.model_dump(mode="json") for key, value in case.state.schedules.items()
    }
    return {
        "status": result.status,
        "config": config,
        "mode": "production" if config == "A5" else "baseline",
        "receipt": result.receipt.model_dump(mode="json") if result.receipt else None,
        "analysis_receipt": result.analysis_receipt.model_dump(mode="json")
        if getattr(result, "analysis_receipt", None)
        else None,
        "confirmation_requested": bool(approvals)
        if config == "A5"
        else result.confirmation_requested,
        "mfa_attempted": mfa_attempted,
        "rule_ids": rules,
        "initial_state": case.initial.model_dump(mode="json"),
        "final_state": final,
        "bank_effects": [*final["events"], *local_effects(case.state)]
        if config == "A5"
        else final["events"],
        "gateway_ledger": [
            item.model_dump(mode="json")
            for item in case.state.ledger
            if item.kind in LOCAL_WRITE_KINDS
        ]
        if config == "A5"
        else [],
        "bank_unknown": result.status == "unknown",
        "actual_actions": actions,
        "approvals": approvals,
        "layer_status": {
            layer: {
                "status": "evaluated" if config == "A5" else "not_evaluable",
                "reason": "production path" if config == "A5" else "baseline disables layer",
            }
            for layer in LAYERS
        },
    }
