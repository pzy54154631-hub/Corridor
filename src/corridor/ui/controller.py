"""One isolated CCA demo session and explicit device-gesture orchestration.

Unsigned previews use separate temporary state. Only confirmation callbacks can
produce input/operation/SPA signatures; every effect goes through the gateway.
"""

from collections.abc import Callable
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from corridor.agent.birthday_event import (
    birthday_candidates,
    derive_dates,
    event_payload,
    lock_window_open,
    resolve_birthday,
)
from corridor.agent.dialogue import Conversation
from corridor.agent.llm import LLMClient
from corridor.bench.bank_cases import BankCase
from corridor.bench.billing_runner import prepare_analysis
from corridor.bench.investment_runner import prepare_investment, prepare_redemption
from corridor.bench.planning_runner import birthday_prepared, prepare_birthday, prepare_funds
from corridor.crypto.jcs import H
from corridor.gateway.mfa import MFAVerifier
from corridor.gateway.plans import project_runtime
from corridor.gateway.privacy import PIIVault
from corridor.gateway.runtime import create_gateway
from corridor.gateway.service import GatewayResult, Proposal
from corridor.kernel.binding import dependency_hashes
from corridor.kernel.check import check
from corridor.kernel.investment import evaluate_investment, risk_retake_errors
from corridor.schemas.bank import (
    CardChangeRequest,
    InputTerm,
    OperationAuthorization,
    ScheduledPaymentAuthorization,
    StateTerm,
    UserInput,
)
from corridor.schemas.domain import Card, MarketingMetadata, Position
from corridor.schemas.money import fraction_string
from corridor.schemas.narrative import FactRef, Narrative
from corridor.sim.billing import report_state, subscription_state
from corridor.sim.device import Device
from corridor.sim.fixtures import make_state
from corridor.sim.keys import signing_key, stable_id
from corridor.sim.showcase import with_spouse_birthday
from corridor.stores.bank import BankStore, add_seconds, available
from corridor.ui import display_names, scam_friction
from corridor.ui.scam_gate import ScamGate

OPERATOR_FALLBACK_TARGET = "2026-10-18T02:00:00Z"


def demo_state(*, showcase=False):
    """Keep frozen runner defaults; opt the interactive UI into extra demo assets."""
    state, bills, subscriptions = make_state(), report_state(), subscription_state()
    state = state.model_copy(
        update={
            "transactions": {
                **state.transactions,
                **bills.transactions,
                **subscriptions.transactions,
            },
            "merchants": {**state.merchants, **bills.merchants, **subscriptions.merchants},
            "subscriptions": subscriptions.subscriptions,
        }
    )
    if not showcase:
        return state
    state = with_spouse_birthday(state)
    return state.model_copy(
        update={
            "cards": {
                **state.cards,
                "CARD_UI_DEMO": Card(
                    id="CARD_UI_DEMO",
                    owner="C_DEMO",
                    account_ref="A_CURRENT",
                    status="active",
                    limit="2000/1",
                    restrictions=["overseas"],
                    card_token="SYNTHETIC_UI_CARD",
                ),
            },
            "positions": {
                **state.positions,
                "POSITION_UI_DEMO": Position(
                    id="POSITION_UI_DEMO",
                    owner="C_DEMO",
                    product_ref="P_A",
                    principal_amount="500/1",
                    units="500/1",
                    reserved_units="0/1",
                    nav="1/1",
                    acquired_at="2026-08-01T00:00:00Z",
                    redeemable_at="2026-09-01T00:00:00Z",
                    status="active",
                ),
            },
        }
    )


class _UnsignedDevice:
    """Build exact wire bodies with an unmistakably invalid signature; never sign."""

    principal = "C_DEMO"

    def confirm_input(self, *, intent_id, slot, value, now, step=1, supersedes=None):
        return UserInput.model_validate({
            "id": stable_id(intent_id, step, f"input-{slot}"),
            "principal": self.principal,
            "batch_id": stable_id(intent_id, 0, "batch"),
            "slot": slot,
            "value": value,
            "submitted_at": now,
            "scope_id": intent_id,
            "supersedes": supersedes,
            "sig": "0" * 128,
        })

    def authorize(self, body):
        return OperationAuthorization.model_validate({
            **body,
            "principal": self.principal,
            "sig": "0" * 128,
        })

    def sign_spa(self, body):
        return ScheduledPaymentAuthorization.model_validate({
            **body,
            "principal": self.principal,
            "sig": "0" * 128,
        })


class _DraftGateway:
    """Collect draft inputs in an isolated snapshot, never in the live store."""

    def __init__(self, gateway):
        self.live = gateway
        self.store = BankStore(gateway.store.snapshot())
        self.inputs: list[UserInput] = []
        self.authorizations = []
        self.plans = []

    def submit_input(self, item):
        self.inputs.append(item)
        self.store.update(user_inputs={**self.store.snapshot().user_inputs, item.id: item})
        return GatewayResult("accepted")

    def submit_authorization(self, item):
        self.authorizations.append(item)
        self.store.update(authorizations={**self.store.snapshot().authorizations, item.id: item})
        return GatewayResult("accepted")

    def submit_plan(self, plan):
        self.plans.append(plan)
        return GatewayResult("registered")

    def propose(self, action, cert):
        card = self.live.preview(
            action, cert, inputs=self.inputs, authorizations=self.authorizations
        )
        # This remains an actual rejecting verdict for unsigned drafts. Its card
        # comes from the separate preview API, never from a fabricated Accept.
        verdict = check(action, cert, self.store.snapshot(), phase="propose")
        return Proposal(verdict, card, H(action), H(cert))


class _BuilderGateway:
    """Retain a stable prompt identity across builder, display and MFA rechecks."""

    def __init__(self, gateway):
        self.live = gateway

    def __getattr__(self, name):
        return getattr(self.live, name)

    def propose(self, action, cert):
        return self.live.propose(action, cert, request_id=action.intent_id)


class _Builder(BankCase):
    def __init__(self, controller, request_id, *, draft=False):
        self.task_id, self.subcase_id = "DEMO", request_id
        self.identity = f"DEMO:{request_id}"
        self.gateway = (
            _DraftGateway(controller.gateway) if draft else _BuilderGateway(controller.gateway)
        )
        self.device = _UnsignedDevice() if draft else controller.device
        self.session = controller.session
        self.session_authorization = controller.session_authorization
        self.initial = self.state
        self.preparations = []
        self.evidence_dir = None


class DemoController:
    def __init__(self, *, evidence_dir: Path | None = None, initial_state=None, device=None):
        self.device = device or Device()
        self.session = stable_id("DEMO", 0, "device-session")
        self.gateway = create_gateway(
            BankStore(initial_state if initial_state is not None else demo_state()),
            bank_key=signing_key("bank"),
            gateway_key=signing_key("gateway"),
            risk_key=signing_key("risk"),
            mfa=MFAVerifier(signing_key("mfa")),
            vault=PIIVault({
                "NAME_CHEN": "Chen",
                "NAME_LIN": "Lin",
                "NAME_NEW": "New",
                "PHONE_CHEN": "13900001001",
                "PHONE_LIN_A": "13900001002",
                "PHONE_LIN_B": "13900001003",
                "PHONE_NEW": "13900001004",
                "NAME_SPOUSE": "Qiao",
                "PHONE_SPOUSE": "13900001005",
            }),
            evidence_dir=evidence_dir,
        )
        self.client = LLMClient(
            mode="scripted", env_file=None, context_guard=self.gateway.vault.ensure_safe
        )
        self.chat = Conversation(
            self.gateway,
            self.client,
            principal=self.device.principal,
            session_id=self.session,
            task_id="DEMO",
        )
        self.session_authorization = None
        self.workflow = None
        self._counter = 0
        self._drafts: dict[str, dict[str, Any]] = {}
        self._pending: dict[str, dict[str, Any]] = {}
        self._plans: dict[str, dict[str, Any]] = {}
        self._results: dict[str, dict[str, Any]] = {}
        self._requests: dict[str, Any] = {}
        self._request_facts: dict[str, str] = {}
        # S7: actions whose last gesture was rejected without a stored result; product
        # layer only (the transaction boundary), never a state or an audit field.
        self._closed: set[str] = set()
        self._last_comparison: dict[str, Any] | None = None
        self._comparison_action = None
        self._objective = "net_return"
        # S6: the display-layer recipient check, an explicit per-action state machine.
        # Answers live only here, bound to the action instance, the identity of its
        # authoritative card and an answering epoch; they never enter a draft, a prepared
        # action, a certificate, a card or the audit chain.
        self._scam_gate = ScamGate()
        # C2: the user's words that created an action (product layer only), so a gesture
        # that reaches an entry point before any page drew the questions is graded on the
        # same words the page would use.
        self._scam_words: dict[str, str] = {}

    @property
    def state(self):
        return self.gateway.store.snapshot()

    # --- S7 planner source (product layer; the kernel and gateway never see it) -------

    @property
    def planner_source(self):
        """``scripted`` or ``<mode>: <model>``; display only."""
        if self.client.mode == "scripted":
            return "scripted"
        return f"{self.client.mode}: {self.client.model}"

    def open_actions(self):
        """Drafts and pending actions the user can still confirm, verify or complete.

        A settled, kernel-refused or device-rejected action is finished; so is a clarify
        draft that can never become a card (no recipient candidate, no amount or no
        source account). A clarify draft whose recipient can still be chosen is open: the
        choice leads to a card that the planner is called again to confirm.
        """
        open_ids = []
        for key, draft in self._drafts.items():
            if key in self._results or key in self._pending or key in self._closed:
                continue
            candidate = draft["candidate"]
            choosable = (
                candidate.status == "clarify"
                and bool(candidate.contact_refs)
                and bool(candidate.amount)
                and bool(candidate.source_ref)
            )
            if "display" in draft or choosable:
                open_ids.append(key)
        open_ids.extend(
            key for key in self._pending if key not in self._results and key not in self._closed
        )
        return open_ids

    def at_transaction_boundary(self):
        """True while no open draft or pending action exists: the only time the planner
        may change, so one action is never proposed by one planner and confirmed under
        another. Finished actions (settled, refused, rejected) never hold it."""
        return not self.open_actions()

    def _finish_if_rejected(self, identity, result):
        """A device-rejected gesture is final for this action (the workflow caches it)."""
        if isinstance(result, dict) and result.get("status") == "rejected":
            self._closed.add(identity)
        return result

    def set_client(self, client, *, minimize_contacts: bool = False):
        """Replace the planner client for the chat; the gateway, kernel and audit are
        untouched. Refused during a transaction."""
        if client is self.client:
            return
        if not self.at_transaction_boundary():
            raise ValueError("Planner cannot change during a transaction")
        self.client = client
        self.chat.client = client
        self.chat.minimize_contacts = minimize_contacts

    # --- S6 display-layer gate (product-layer session state only) ---------------------
    #
    # The gate is checked at every entry point that can request an SMS challenge, confirm
    # an approval or submit a dispatch: confirm_transfer (after select_contact too),
    # approve_pending, request_mfa, complete_mfa, confirm_plan and resume. The pages
    # reach these through DomesticSession.confirm / verify_mfa, IMAdapter.click, the
    # manual MFA form, the device page's approve button, the transfer page, the
    # compensation button and the demo presets; all of them meet the same check. Cancel
    # (stop), handoff, previews, select_contact and advance_clock are not gated.

    def _authoritative_cards(self, identity):
        """The controller's own current card(s) for an action; never a page's copy."""
        pending = self._pending.get(identity)
        if pending is not None:
            return self._pending_cards(pending)
        draft = self._drafts.get(identity)
        if draft is not None and "display" in draft:
            return [draft["display"].card]
        plan = self._plans.get(identity)
        if plan is not None:
            return list(plan["display"].cards.values())
        return []

    def scam_contact(self, card):
        """The trusted registry entry a card pays, and its facts for the context snapshot."""
        objects = card.get("objects") if isinstance(card, dict) else None
        reference = objects.get("contact_ref") if isinstance(objects, dict) else None
        contacts = getattr(self.state, "contacts", None)
        contact = contacts.get(reference) if isinstance(contacts, dict) and reference else None
        facts = None
        if contact is not None:
            facts = {
                "id": getattr(contact, "id", reference),
                "is_new": bool(getattr(contact, "is_new", False)),
                "verified_at": getattr(contact, "verified_at", None),
            }
        return contact, {"contact_ref": reference, "contact": facts}

    def _scam_grade(self, cards, text, external_files):
        """Level, cues and the context snapshot the level was computed from."""
        files = int(external_files or 0)
        words = text if isinstance(text, str) and text else None
        grade, codes, contacts = 0, [], []
        for card in cards:
            contact, facts = self.scam_contact(card)
            contacts.append(facts)
            grade = max(
                grade,
                scam_friction.level(card, contact=contact, text=words, external_files=files),
            )
            for code in scam_friction.cues(card, contact=contact, text=words, external_files=files):
                if code not in codes:
                    codes.append(code)
        context = {
            "text_hash": H(words) if words is not None else None,
            "external_files": files,
            "contacts": contacts,
        }
        return grade, tuple(codes), context

    def scam_gate_establish(self, identity, *, text=None, external_files=0, origin=None):
        """Establish the gate for an action before its questions are rendered; idempotent.

        The card identity comes from the action's authoritative card and the level from
        the context snapshot (the user's words, the attachment count, the contact facts);
        a caller cannot pass either. Returns the gate status, or None when the action
        was never gated and needs no questions (level 0): that path is unchanged. An
        action that was gated once stays gated: a re-grade that computes level 0 (the
        payee switched to a known contact, the words went missing) is stored as level 1
        by the state machine, with blank answers, so the questions are asked again and
        the entry points keep refusing until they are answered.
        """
        cards = self._authoritative_cards(identity)
        current = scam_friction.cards_identity(cards)
        if current is None:
            return self.scam_gate_status(identity)
        grade, codes, context = self._scam_grade(cards, text, external_files)
        if self._scam_gate.entry(identity) is None and grade == 0:
            return None
        self._scam_gate.establish(
            identity,
            card_identity=current,
            level=grade,
            context=context,
            cues=codes,
            origin=origin,
            # R7: words saying someone told the user to pay preset "someone is guiding".
            preset=scam_friction.preset(text),
        )
        return self.scam_gate_status(identity)

    def scam_gate_answer(self, identity, *, card_identity, epoch, answers):
        """Apply an answer event that matches the action, card identity and epoch.

        A stale event (another card, an earlier epoch, a latched pause, no gate) changes
        nothing; its reason is recorded. Returns the current status either way.
        """
        self._scam_gate.answer(identity, card_identity=card_identity, epoch=epoch, answers=answers)
        return self.scam_gate_status(identity)

    def scam_gate_retry(self, identity, *, card_identity, epoch):
        """The user's "answer again" gesture for the current epoch of a paused gate."""
        self._scam_gate.retry(identity, card_identity=card_identity, epoch=epoch)
        return self.scam_gate_status(identity)

    def scam_gate_status(self, identity):
        """A copy of the gate record plus its live standing against the current card."""
        entry = self._scam_gate.entry(identity)
        if entry is None:
            return None
        current = scam_friction.cards_identity(self._authoritative_cards(identity))
        pending = self._pending.get(identity)
        return {
            **entry,
            "identity": identity,
            "current_card_identity": current,
            "stale": current is None or current != entry["card_identity"],
            "reason": self._scam_gate.refusal_reason(identity, current),
            "challenge_issued": bool(pending and pending.get("challenge_id")),
        }

    def scam_gate_rejections(self):
        """The stale gate events refused so far (display and tests only)."""
        return self._scam_gate.rejections()

    def _scam_gate_inherit(self, source, identity):
        """Carry the gate of a replaced action instance over to its replacement.

        A redisplay of a draft-mode pending previews the same intent again under a new
        pending id; the questions were shown for the old id, so the new id must not be
        reachable by a direct gesture (a session replay, the IM adapter) before the page
        redraws it. The replacement starts ``incomplete`` at the old level, bound to its
        own card; the old record stays and reads as stale. Without a gate on the old id
        nothing is created.
        """
        if identity and identity != source and source in self._scam_words:
            # C2: the replacement is graded on the same words if it is ever gated anew.
            self._scam_words.setdefault(identity, self._scam_words[source])
        if not identity or identity == source or self._scam_gate.entry(source) is None:
            return None
        current = scam_friction.cards_identity(self._authoritative_cards(identity))
        return self._scam_gate.inherit(source, identity, card_identity=current)

    def remember_words(self, identity, text):
        """C2: keep the user's words for an action the chat prepared through a form's
        preparer (a scheduled transfer), for the recipient check's grading."""
        if identity and isinstance(text, str) and text:
            self._scam_words[identity] = text

    def _scam_required(self, identity):
        """C2: establish the gate at the first gesture when no page did, if the action
        needs it by its own controlled facts (a new payee, or authority / external cues
        in the words that created it). Level 0 stays ungated, exactly as before."""
        if self._scam_gate.entry(identity) is not None:
            return
        cards = self._authoritative_cards(identity)
        if scam_friction.cards_identity(cards) is None:
            return
        words = self._scam_words.get(identity)
        grade, codes, context = self._scam_grade(cards, words, 0)
        if grade == 0:
            return
        self._scam_gate.establish(
            identity,
            card_identity=scam_friction.cards_identity(cards),
            level=grade,
            context=context,
            cues=codes,
            origin=None,
            preset=scam_friction.preset(words),
        )

    def _scam_refusal(self, identity):
        """A displayable refusal while an established gate does not allow continuing.

        Fail closed: once the questions have been shown for an action, an unanswered
        check refuses like a paused one (``scam_incomplete`` / ``scam_pause``), and a
        card that changed, vanished or has no computable identity refuses as
        ``scam_stale``; a stale button event, a rerun, a page change or a gesture sent
        outside the page in the same session cannot continue past it. C2 (review of R7):
        an action that needs the questions by its own facts is gated at its first gesture
        even when no page drew them (a chat reply confirmed directly, the IM text channel,
        the judge view's legacy form, another page's button), so no entry point is open
        before they are answered. Actions that never need them behave exactly as before.
        """
        self._scam_required(identity)
        current = scam_friction.cards_identity(self._authoritative_cards(identity))
        reason = self._scam_gate.refusal_reason(identity, current)
        if reason is None:
            return None
        messages = {
            scam_friction.REASON: (
                "Paused by the display-layer recipient check: answer its two questions "
                "again before any verification, approval or dispatch."
            ),
            scam_friction.REASON_INCOMPLETE: (
                "Waiting for the display-layer recipient check: answer its two "
                "questions before any verification, approval or dispatch."
            ),
        }
        return {
            "status": scam_friction.STATUS,
            "reason": reason,
            "rule_ids": [],
            "message": messages.get(
                reason,
                "The display-layer recipient check was answered for another card: answer "
                "it again for the current card before any verification, approval or dispatch.",
            ),
        }

    def _pending_cards(self, pending):
        display = pending["display"]
        if pending["mode"] == "draft":
            return list(getattr(display, "cards", {}).values())
        return [display.card]

    def _id(self, kind):
        self._counter += 1
        return stable_id("DEMO", self._counter, kind)

    def _require_login(self):
        if (
            self.session_authorization is None
            or self.session_authorization.expires_at <= self.state.now
        ):
            raise ValueError("Enter the simulated bank and confirm device login first.")

    def _claim_request(self, request_id, facts):
        digest = H(facts)
        if request_id in self._request_facts and self._request_facts[request_id] != digest:
            raise ValueError("Request ID cannot replace previously displayed facts")
        self._request_facts[request_id] = digest

    def start_session(self):
        from corridor.sim.device_workflow import DeviceWorkflow

        if (
            self.session_authorization is not None
            and self.session_authorization.expires_at > self.state.now
        ):
            return {"status": "ready", "planner_mode": "scripted"}
        if self.session_authorization is not None:
            self.session = self._id("device-session")
            self.chat = Conversation(
                self.gateway,
                self.client,
                principal=self.device.principal,
                session_id=self.session,
                task_id="DEMO",
            )
            self._pending.clear()
            self._drafts.clear()
            self._closed.clear()
            self._requests.clear()
            self._request_facts.clear()
        self.session_authorization = self.device.login(
            session_id=self.session,
            expires_at=add_seconds(self.state.now, 3600),
        )
        result = self.gateway.login(self.session_authorization)
        self.workflow = DeviceWorkflow(self.device)
        return {
            "status": "ready" if result.status == "accepted" else result.status,
            "rule_ids": result.rule_ids,
            "planner_mode": "scripted",
        }

    def _result(self, result):
        receipt = result.receipt.model_dump(mode="json") if result.receipt else None
        analysis = (
            result.analysis_receipt.model_dump(mode="json") if result.analysis_receipt else None
        )
        data = {
            "status": result.status,
            "rule_ids": list(result.rule_ids),
            "receipt": receipt,
            "analysis_receipt": analysis,
            "details": deepcopy(result.details),
            "audit_checkpoint": self.gateway.audit.checkpoint(),
        }
        if result.analysis_receipt:
            refs = [
                FactRef(receipt_ref=result.analysis_receipt.id, field=field)
                for field in result.analysis_receipt.results
            ]
            data["facts"] = self.gateway.render_narrative(
                Narrative(fact_refs=refs), self.device.principal
            ).model_dump(mode="json")
            fields = result.analysis_receipt.results
            if "cancellable_authorizations" in fields:
                data["cancellable"] = list(fields["cancellable_authorizations"].value)
                data["subscriptions"] = [
                    self.state.subscriptions[ref].model_dump(mode="json")
                    for ref in data["cancellable"]
                    if ref in self.state.subscriptions
                ]
                data["items"] = deepcopy(data["subscriptions"])
        elif result.receipt and result.receipt.kind in {
            "transfer",
            "balance_query",
            "purchase_product",
            "redeem_product",
        }:
            fields = ["status", "balance_after"]
            if result.receipt.kind != "balance_query":
                fields.append("amount")
            data["facts"] = self.gateway.render_narrative(
                Narrative(
                    fact_refs=[
                        FactRef(receipt_ref=result.receipt.id, field=field) for field in fields
                    ]
                ),
                self.device.principal,
            ).model_dump(mode="json")
        return data

    def preview_transfer(self, text, *, request_id):
        self._require_login()
        self._claim_request(request_id, {"kind": "transfer", "text_hash": H(text)})
        if request_id in self._requests:
            return deepcopy(self._requests[request_id])
        candidate = self.chat.receive(text, request_id=request_id)
        draft_id = candidate.intent_id
        data = {
            "draft_id": draft_id,
            "status": candidate.status,
            "contacts": deepcopy(candidate.contact_refs),
            "card": {},
            "canonical": True,
            "reasons": deepcopy(candidate.reasons),
        }
        self._drafts[draft_id] = {"candidate": candidate, "request_id": request_id}
        self.remember_words(draft_id, text)
        if candidate.status == "awaiting_input":
            display = self.workflow.show_transfer(self.chat, candidate)
            self._drafts[draft_id]["display"] = display
            data["card"] = deepcopy(display.card)
        self._requests[request_id] = deepcopy(data)
        return data

    def select_contact(self, draft_id, contact_ref):
        draft = self._drafts[draft_id]
        candidate = draft["candidate"]
        if contact_ref not in candidate.contact_refs:
            raise ValueError("The recipient is not in the verified candidate set.")
        display = self.workflow.show_transfer(self.chat, candidate, contact_ref=contact_ref)
        draft["display"] = display
        return {
            "draft_id": draft_id,
            "status": "awaiting_input",
            "contacts": deepcopy(candidate.contact_refs),
            "card": deepcopy(display.card),
            "canonical": True,
        }

    def confirm_transfer(self, draft_id, contact_ref=None, *, request_id=None):
        self._require_login()
        if draft_id in self._results:
            return deepcopy(self._results[draft_id])
        if contact_ref:
            self.select_contact(draft_id, contact_ref)
        draft = self._drafts[draft_id]
        if "display" not in draft:
            return {"status": "clarify", "rule_ids": ["PROV-AMBIGUOUS"]}
        # S6: checked on the card that is current now (after select_contact above): an
        # unanswered, paused or stale gate means no device gesture, proposal or dispatch.
        refusal = self._scam_refusal(draft_id)
        if refusal is not None:
            return {
                **refusal,
                "draft_id": draft_id,
                "card": deepcopy(draft["display"].card),
                "contacts": deepcopy(draft["candidate"].contact_refs),
                "canonical": True,
            }
        confirmed = self.workflow.confirm_transfer(
            self.chat,
            draft["display"].display_id,
            request_id=f"{request_id or draft_id}:{draft['display'].display_id}",
        )
        if confirmed.status == "approved":
            result = self._result(
                self.gateway.dispatch(
                    confirmed.prepared.action, confirmed.prepared.cert, confirmed.token
                )
            )
            self._results[draft_id] = result
            return deepcopy(result)
        if confirmed.status == "waiting_mfa":
            if draft_id in self._pending:
                pending = self._pending[draft_id]
                return {
                    "status": "awaiting_approval" if pending["verified"] else "waiting_mfa",
                    "pending_id": draft_id,
                    "card": deepcopy(pending["display"].card),
                    "canonical": True,
                }
            return self._store_prepared(confirmed.prepared, pending_id=draft_id)
        if confirmed.status == "redisplay" and confirmed.display is not None:
            if confirmed.prepared is not None:
                data = self._store_prepared(confirmed.prepared, pending_id=draft_id)
                self._pending[draft_id]["display"] = confirmed.display
                return {**data, "status": "redisplay"}
            draft["display"] = confirmed.display
        return self._finish_if_rejected(
            draft_id,
            {
                "draft_id": draft_id,
                "status": confirmed.status,
                "rule_ids": list(confirmed.rule_ids),
                "card": deepcopy(confirmed.display.card) if confirmed.display else {},
                "contacts": deepcopy(draft["candidate"].contact_refs),
                "canonical": True,
            },
        )

    def query_balance(self):
        self._require_login()
        return self._result(self.chat.query_balance())

    def bill_report(self, period="2026-09"):
        self._require_login()
        builder = _Builder(self, self._id("report"))
        prepared = prepare_analysis(builder, "bill_report", period=period)
        return self._result(self.gateway.query(prepared.action, prepared.cert))

    def anomaly_query(self, period="2026-09"):
        self._require_login()
        builder = _Builder(self, self._id("anomaly"))
        prepared = prepare_analysis(builder, "anomaly_query", period=period)
        return self._result(self.gateway.query(prepared.action, prepared.cert))

    def _preview_operation(self, factory: Callable, *, request_id):
        from corridor.gateway.preview import PreviewRequest

        if request_id in self._requests:
            return deepcopy(self._requests[request_id])
        draft = _Builder(self, request_id, draft=True)
        prepared = factory(draft)
        request = PreviewRequest(
            prepared.action,
            prepared.cert,
            inputs=draft.gateway.inputs,
            authorizations=draft.gateway.authorizations,
        )
        live = _Builder(self, request_id)
        display = self.workflow.show_plan(
            self.gateway, [request], {prepared.action.node_id: lambda: factory(live)}
        )
        pending_id = request_id
        self._pending[pending_id] = {
            "mode": "draft",
            "display": display,
            "node_id": prepared.action.node_id,
            "factory": factory,
        }
        data = {
            "pending_id": pending_id,
            "status": "awaiting_approval",
            "canonical": True,
            "card": deepcopy(display.cards[prepared.action.node_id]),
        }
        self._requests[request_id] = deepcopy(data)
        return data

    def preview_subscriptions(self, *, request_id=None):
        self._require_login()
        request_id = request_id or self._id("subscriptions")
        self._claim_request(request_id, {"kind": "subscription_query", "period": "2026"})
        return self._preview_operation(
            lambda builder: prepare_analysis(builder, "subscription_query", period="2026"),
            request_id=request_id,
        )

    def cancel_subscription(self, ref, *, request_id):
        self._require_login()
        self._claim_request(request_id, {"kind": "cancel_subscription", "ref": ref})
        if ref not in self.state.subscriptions:
            raise ValueError("No cancellable debit authorization is available.")

        def build(builder):
            item = builder.device.confirm_input(
                intent_id=builder.intent(), slot="subscription", value=ref, now=builder.state.now
            )
            authorization = builder.operation("cancel_subscription", [ref], [item])
            term = StateTerm(
                collection="subscriptions",
                object_ref=ref,
                field="id",
                version=builder.state.subscriptions[ref].version,
            )
            return builder.prepare(
                "cancel_subscription",
                {"authorization_ref": ref},
                {"authorization_ref": term},
                inputs=[item],
                authorization=authorization,
            )

        return self._preview_operation(build, request_id=request_id)

    def cancel_schedule(self, ref, *, request_id):
        """B2: revoke the remaining runs of one of the customer's scheduled authorizations.

        The kernel and gateway already accept the cancel_schedule action through the normal
        approval and dispatch path (no new gateway interface): this only prepares its card,
        unsigned, like the subscription form's cancellation; the device gesture signs it.
        """
        self._require_login()
        spa = self.state.authorizations.get(ref)
        if (
            spa is None
            or getattr(spa, "authorization_type", None) != "spa"
            or spa.principal != self.device.principal
        ):
            # C1: checked before the request id is claimed, so a refused reference never
            # occupies the id a valid choice may use next.
            raise ValueError("No such scheduled payment authorization.")
        self._claim_request(request_id, {"kind": "cancel_schedule", "ref": ref})

        def build(builder):
            item = builder.device.confirm_input(
                intent_id=builder.intent(), slot="schedule_cancel", value=ref, now=builder.state.now
            )
            authorization = builder.operation("cancel_schedule", [ref], [item])
            term = StateTerm(
                collection="authorizations",
                object_ref=ref,
                field="id",
                version=builder.state.authorizations[ref].version,
            )
            return builder.prepare(
                "cancel_schedule",
                {"schedule_ref": ref},
                {"schedule_ref": term},
                inputs=[item],
                authorization=authorization,
            )

        return self._preview_operation(build, request_id=request_id)

    def _store_prepared(self, prepared, *, pending_id=None):
        pending_id = pending_id or prepared.action.action_id
        display = self.workflow.show_prepared(self.gateway, prepared)
        self._pending[pending_id] = {
            "mode": "prepared",
            "prepared": prepared,
            "display": display,
            "challenge_id": None,
            "verified": False,
        }
        verdict = prepared.proposal.verdict
        if verdict.outcome == "Reject":
            # S4 / R6: a kernel Reject is final for this proposal (an SMS code cannot fix a
            # rule error), so it is reported as refused, not as waiting for MFA. Recording
            # the result retires it from the device page's pending list; dispatch stays 0.
            result = {
                "pending_id": pending_id,
                "status": "rejected",
                "outcome": verdict.outcome,
                "rule_ids": list(verdict.rule_ids),
                "card": deepcopy(display.card),
                "canonical": True,
            }
            self._results[pending_id] = deepcopy(result)
            return result
        return {
            "pending_id": pending_id,
            "status": "waiting_mfa" if verdict.tier == "red" else "awaiting_approval",
            "card": deepcopy(display.card),
            "canonical": True,
        }

    def kernel_refusal(self, pending_id):
        """The kernel's Reject for a stored proposal, for display; None if not refused."""
        pending = self._pending.get(pending_id)
        if not pending or pending.get("mode") != "prepared":
            return None
        prepared = pending["prepared"]
        verdict = prepared.proposal.verdict
        if verdict.outcome != "Reject":
            return None
        action_hash = H(prepared.action)
        # This action's own bank dispatches, read from the audit chain (0 for a refusal).
        dispatches = sum(
            1
            for row in self.gateway.audit.records()
            if row["event"] == "dispatch_intent"
            and row.get("payload", {}).get("action_hash") == action_hash
        )
        return {
            "outcome": verdict.outcome,
            "rule_ids": list(verdict.rule_ids),
            "dispatches": dispatches,
        }

    def preview_password_change(self, *, request_id=None):
        """Preview credential-version control without collecting any password."""
        self._require_login()
        request_id = request_id or self._id("password-change")
        self._claim_request(request_id, {"kind": "password_change_request"})

        def build(builder):
            principal = builder.device.principal
            item = builder.device.confirm_input(
                intent_id=builder.intent(),
                slot="principal",
                value=principal,
                now=builder.state.now,
            )
            authorization = builder.operation("password_change_request", [principal], [item])
            return builder.prepare(
                "password_change_request",
                {"principal": principal},
                {
                    "principal": StateTerm(
                        collection="customers",
                        object_ref=principal,
                        field="id",
                        version=builder.state.customers[principal].version,
                    )
                },
                inputs=[item],
                authorization=authorization,
            )

        return self._preview_operation(build, request_id=request_id)

    def preview_card_operation(
        self, kind, *, card_ref=None, change=None, account_ref="A_CURRENT", request_id=None
    ):
        """Display unsigned card facts; only the later device gesture signs them."""
        self._require_login()
        state_changes = {
            "freeze_card": "frozen",
            "unfreeze_card": "active",
            "report_card_lost": "lost",
            "restore_card": "frozen",
        }
        changes = {
            "lower_card_limit",
            "raise_card_limit",
            "restrict_card",
            "relax_card_restriction",
        }
        if kind not in {"apply_virtual_card", *state_changes, *changes}:
            raise ValueError("Unsupported card operation")
        request_id = request_id or self._id(kind)
        value = None
        if kind == "apply_virtual_card":
            if card_ref is not None or change is not None:
                raise ValueError("Card application accepts only an owned account")
        else:
            payload = {"status": state_changes[kind]} if kind in state_changes else change
            if not isinstance(payload, dict) or set(payload) & {"card_ref", "kind"}:
                raise ValueError("Card controls require explicit resulting facts")
            if kind in state_changes and change is not None:
                raise ValueError("State operation has a fixed resulting status")
            value = CardChangeRequest.model_validate({
                **deepcopy(payload),
                "card_ref": card_ref,
                "kind": kind,
            }).model_dump(mode="json", exclude_none=True)
        self._claim_request(
            request_id,
            {"kind": kind, "account_ref": account_ref, "card_ref": card_ref, "change": value},
        )

        def build(builder):
            ref = account_ref if kind == "apply_virtual_card" else card_ref
            collection = "accounts" if kind == "apply_virtual_card" else "cards"
            field = "account_ref" if kind == "apply_virtual_card" else "card_ref"
            obj = getattr(builder.state, collection)[ref]
            if obj.owner != builder.device.principal:
                raise ValueError("Card object belongs to another customer")
            item = builder.device.confirm_input(
                intent_id=builder.intent(),
                slot="card_application" if value is None else "card_change",
                value={"account_ref": ref} if value is None else deepcopy(value),
                now=builder.state.now,
            )
            authorization = builder.operation(kind, [ref], [item])
            args = {field: ref}
            terms = {
                field: StateTerm(
                    collection=collection, object_ref=ref, field="id", version=obj.version
                )
            }
            if value is not None:
                args["change_ref"] = item.id
                terms["change_ref"] = InputTerm(input_ref=item.id, slot="card_change")
            return builder.prepare(kind, args, terms, inputs=[item], authorization=authorization)

        return self._preview_operation(build, request_id=request_id)

    def approve_pending(self, pending_id):
        self._require_login()
        if pending_id in self._results:
            return deepcopy(self._results[pending_id])
        pending = self._pending[pending_id]
        refusal = self._scam_refusal(pending_id)
        if refusal is not None:
            # S6: paused; the stored proposal, challenge and verification stay as they are.
            return {
                **refusal,
                "pending_id": pending_id,
                "card": deepcopy(self._pending_cards(pending)[0]),
                "canonical": True,
            }
        if pending["mode"] == "draft":
            confirmation = self.workflow.confirm_plan(
                self.gateway,
                pending["display"].display_id,
                selected_nodes=[pending["node_id"]],
                request_id=pending_id,
            )
            if confirmation.results:
                result = self._result(confirmation.results[pending["node_id"]])
                self._results[pending_id] = result
                return deepcopy(result)
            if confirmation.status == "waiting_mfa":
                prepared = confirmation.prepared.get(pending["node_id"])
                if prepared is not None:
                    return self._store_prepared(prepared, pending_id=pending_id)
            if confirmation.status == "redisplay":
                replacement = (
                    self.compensate(pending["compensation_plan_id"])
                    if "compensation_plan_id" in pending
                    else self._preview_operation(
                        pending["factory"], request_id=self._id("redisplay")
                    )
                )
                # S6: the replacement is a new pending id for the same intent; it
                # inherits the gate (incomplete) before the old id is retired, so no
                # gesture reaches it ungated between this return and the page redraw.
                self._scam_gate_inherit(pending_id, replacement.get("pending_id"))
                self._pending.pop(pending_id)
                if replacement["status"] == "no_compensation":
                    return replacement
                return {**replacement, "status": "redisplay", "replacement": replacement}
            return self._finish_if_rejected(
                pending_id, {"status": confirmation.status, "rule_ids": confirmation.rule_ids}
            )
        prepared = pending["prepared"]
        if prepared.proposal.verdict.tier == "red" and not pending["verified"]:
            return {
                "status": "waiting_mfa",
                "pending_id": pending_id,
                "rule_ids": ["TIER-MFA-REQUIRED"],
            }
        confirmation = self.workflow.confirm_prepared(
            self.gateway,
            pending["display"].display_id,
            request_id=f"{pending_id}:final:{pending['display'].display_id}",
            prepared=prepared,
        )
        if confirmation.status != "approved":
            if confirmation.display is not None:
                pending["display"] = confirmation.display
            return self._finish_if_rejected(
                pending_id, {"status": confirmation.status, "rule_ids": confirmation.rule_ids}
            )
        dispatch = (
            self.gateway.device_control
            if prepared.action.kind == "password_change_request"
            else self.gateway.dispatch
        )
        result = self._result(dispatch(prepared.action, prepared.cert, confirmation.token))
        self._results[pending_id] = result
        return deepcopy(result)

    def request_mfa(self, pending_id):
        self._require_login()
        pending = self._pending[pending_id]
        refusal = self._scam_refusal(pending_id)
        if refusal is not None:
            # S6: no SMS challenge is issued while the recipient check is not passed.
            return {**refusal, "pending_id": pending_id}
        if pending["mode"] == "draft":
            return {"status": "intent_confirmation_required", "pending_id": pending_id}
        prepared = pending["prepared"]
        refusal = self.kernel_refusal(pending_id)
        if refusal is not None:
            # R6: no SMS code is ever requested for a proposal the kernel refused.
            return {
                "status": "rejected",
                "pending_id": pending_id,
                "rule_ids": refusal["rule_ids"],
            }
        if pending["challenge_id"] is None:
            pending["challenge_id"] = self.gateway.request_mfa(
                prepared.action, prepared.cert, request_id=pending_id
            )
        return {
            "status": "waiting_mfa",
            "challenge_id": pending["challenge_id"],
            "pending_id": pending_id,
        }

    def complete_mfa(self, pending_id, *, code=None):
        self._require_login()
        pending = self._pending[pending_id]
        refusal = self._scam_refusal(pending_id)
        if refusal is not None:
            # S6: a typed code is not verified while the check is not passed; attempts
            # are not consumed and a stored verification is not reported.
            return {**refusal, "pending_id": pending_id}
        if pending["verified"]:
            return {"status": "mfa_verified", "pending_id": pending_id}
        challenge = pending.get("challenge_id")
        if challenge is None:
            return {"status": "waiting_mfa", "rule_ids": ["MFA-INVALID"]}
        grant = (
            self.device.complete_mfa(self.gateway.mfa, challenge, now=self.state.now)
            if code is None
            else self.gateway.mfa.verify(challenge, code, now=self.state.now)
        )
        if grant is None or self.gateway.submit_mfa(grant).status != "accepted":
            return {"status": "rejected", "rule_ids": ["MFA-INVALID"]}
        prepared = pending["prepared"]
        cert = prepared.cert.model_copy(update={"mfa_grant_refs": [grant.grant_id]})
        cert = cert.model_copy(
            update={"dependency_hashes": dependency_hashes(prepared.action, cert, self.state)}
        )
        updated = replace(
            prepared,
            cert=cert,
            proposal=self.gateway.propose(
                prepared.action, cert, request_id=prepared.action.intent_id
            ),
        )
        pending["prepared"], pending["verified"] = updated, True
        return {"status": "mfa_verified", "pending_id": pending_id}

    def risk_retake_blocked(self) -> bool:
        """Read the current assessment limits without signing or recording a verdict."""
        return bool(risk_retake_errors(self.device.principal, self.state))

    def submit_risk_answers(self, answers):
        """Sign explicit device answers through the existing assessment gateway."""
        return self._result(self._submit_risk_answers(answers))

    def _submit_risk_answers(self, answers):
        self._require_login()
        builder = _Builder(self, self._id("questionnaire"))
        signed = self.device.confirm_input(
            intent_id=builder.intent(),
            slot="answers",
            value=answers,
            now=self.state.now,
        )
        result = self.gateway.submit_input(signed)
        if result.status != "accepted":
            return result
        prepared = builder.prepare(
            "risk_assess",
            {"answers_input_ref": signed.id},
            {"answers_input_ref": InputTerm(input_ref=signed.id, slot="answers")},
            inputs=[signed],
        )
        result = self.gateway.query(prepared.action, prepared.cert)
        return result

    def compare_investments(self, objective="net_return"):
        self._require_login()
        builder = _Builder(self, self._id("compare"))
        prepared, _ = prepare_investment(builder, changes={"objective": objective})
        result = self._result(self.gateway.query(prepared.action, prepared.cert))
        result["bic"] = (
            prepared.cert.best_interest.model_dump(mode="json")
            if prepared.cert.best_interest
            else None
        )
        if result["bic"]:
            result["rows"] = deepcopy(result["bic"]["rows"])
            result["candidate_ids"] = [row["product_id"] for row in result["rows"]]
            result["exclusions"] = deepcopy(result["bic"].get("exclusions", []))
        self._last_comparison, self._objective = deepcopy(result), objective
        self._comparison_action = prepared.action
        result["exclusions"] = deepcopy(evaluate_investment(prepared.action, self.state).exclusions)
        return result

    def marketing_twin(self, sponsored):
        """Recompute a detached comparison; marketing never enters trusted state."""
        self._require_login()
        if self._comparison_action is None or self._last_comparison is None:
            return {"status": "comparison_required"}
        metadata = MarketingMetadata(
            product_id="P_B",
            sponsored=sponsored,
            campaign_text_handle="PROMO" if sponsored else "NONE",
        )
        detached = self.state.model_copy(deep=True)
        evaluation = evaluate_investment(self._comparison_action, detached)
        certificate = evaluation.certificate
        bic = certificate.model_dump(mode="json") if certificate is not None else None
        previous = self._last_comparison["bic"]
        unchanged = bool(
            bic
            and previous
            and all(
                bic[field] == previous[field]
                for field in ("candidate_ids", "rows", "chosen_product_id")
            )
        )
        return {
            "status": "validated",
            "unchanged": unchanged,
            "bic": bic,
            "marketing": metadata.model_dump(mode="json"),
            "catalog_hash": H(detached.catalog),
            "planner_mode": "scripted",
            "synthetic": True,
        }

    def purchase_terms(self, product_ref, *, amount, objective=None, reason_code="user_choice"):
        """C1 (R-10): every preference the purchase gesture would sign, without its id.

        Built by the wealth page's own preference builder on a draft copy (nothing is
        signed or stored), so the chat and IM reviews list exactly what the confirm
        gesture signs, and the confirm can refuse terms that changed after review.
        """
        from corridor.bench.investment_runner import _preference

        changes = {
            "objective": objective or self._objective,
            "amount": amount,
            "selection_mode": "explicit",
            "selected_product_ref": product_ref,
            "reason_code": reason_code,
        }
        # The builder reads only the case identity (for the preference id, dropped here).
        preference = _preference(SimpleNamespace(identity="DEMO:purchase-terms"), changes)
        terms = preference.model_dump(mode="json")
        terms.pop("id", None)
        return terms

    def preview_investment(
        self,
        product_ref=None,
        *,
        reason_code="user_choice",
        request_id=None,
        amount=None,
        objective=None,
    ):
        """Sign the purchase preference and prepare the purchase card (a device gesture).

        The wealth page's button and the chat's confirm button (B1) both call this; the
        chat passes the amount and objective its review showed. Defaults are unchanged.
        """
        self._require_login()
        request_id = request_id or self._id("purchase")
        objective = objective or self._objective
        facts = {
            "kind": "purchase_product",
            "product_ref": product_ref,
            "objective": objective,
            "reason_code": reason_code,
        }
        if amount is not None:
            facts["amount"] = amount
        self._claim_request(request_id, facts)
        if request_id in self._requests:
            return deepcopy(self._requests[request_id])
        builder = _Builder(self, request_id)
        changes = {"objective": objective}
        if amount is not None:
            changes["amount"] = amount
        if product_ref is not None:
            changes.update(
                selection_mode="explicit", selected_product_ref=product_ref, reason_code=reason_code
            )
        prepared, _ = prepare_investment(
            builder, purchase=True, changes=changes, chosen=product_ref
        )
        data = self._store_prepared(prepared)
        self._requests[request_id] = deepcopy(data)
        return data

    def redeem(self, position_ref, amount=None, *, request_id=None):
        self._require_login()
        position = self.state.positions.get(position_ref)
        if position is None or position.owner != self.device.principal:
            raise ValueError("No eligible position is available for this customer.")
        value = amount or fraction_string(position.units - position.reserved_units)
        request_id = request_id or self._id("redemption")
        self._claim_request(
            request_id, {"kind": "redeem_product", "position_ref": position_ref, "amount": value}
        )
        return self._preview_operation(
            lambda builder: prepare_redemption(builder, value, position_ref=position_ref),
            request_id=request_id,
        )

    def preview_scheduled_transfer(
        self, source, contact, amount, request_id, *, date_text, monthly_day=None
    ):
        """Delegate to the transfer form's own preparer with explicit fields."""
        from corridor.ui import operations

        if source is None:
            self._require_login()
            accounts, _ = operations._choices(self)
            # Chat never chooses between funding accounts: only a sole current account.
            if accounts != ["A_CURRENT"]:
                raise ValueError("Choose the funding account on the transfer page.")
            source = accounts[0]
        return operations.preview_scheduled_transfer(
            self, source, contact, amount, request_id, date_text=date_text, monthly_day=monthly_day
        )

    def preview_aa_collection(self, amount, participants, *, request_id=None):
        """Delegate to the split-bill form's own preparer with explicit participants."""
        from corridor.ui import operations

        return operations.preview_aa_collection(self, amount, participants, request_id=request_id)

    def query_transactions(self):
        """Delegate to the billing page's own green transaction-history query."""
        from corridor.ui import operations

        return operations.query_transactions(self)

    def _birthday_candidate(self, item):
        return {**event_payload(item), "lock_window_open": lock_window_open(item, self.state.now)}

    def birthday_candidates(self):
        """Trusted calendar events with derived dates; display data, never authority."""
        return [
            self._birthday_candidate(item)
            for item in birthday_candidates(self.gateway, self.device.principal)
        ]

    def preview_birthday(
        self, *, selected_indices=None, request_id=None, event_ref=None, relation=None
    ):
        from corridor.gateway.preview import PreviewRequest

        self._require_login()
        selected = list(range(4)) if selected_indices is None else list(selected_indices)
        if not selected or selected != list(range(len(selected))) or len(selected) > 4:
            return {
                "status": "selection_required",
                "rule_ids": ["PLAN-SCOPE"],
                "nodes": [],
                "message": (
                    "Select each task and its prerequisites; unchecked items receive no authority."
                ),
            }
        # One trusted calendar event is chosen before any draft input exists. Several
        # candidates are listed for the user; the controller never guesses between them.
        resolution = resolve_birthday(
            self.gateway, self.device.principal, event_ref=event_ref, relation=relation
        )
        if resolution.event is None:
            return {
                "status": resolution.status,
                "rule_ids": [],
                "nodes": [],
                "candidates": [self._birthday_candidate(item) for item in resolution.candidates],
            }
        event = resolution.event
        if not lock_window_open(event, self.state.now):
            return {
                "status": "outside_lock_month",
                "rule_ids": [],
                "nodes": [],
                "event": event_payload(event),
            }
        request_id = request_id or self._id("birthday")
        self._claim_request(
            request_id,
            {
                "kind": "birthday",
                "selected_indices": selected,
                "event_ref": event.event_ref,
                "order_due": event.order_due,
            },
        )
        if request_id in self._requests:
            return deepcopy(self._requests[request_id])
        draft = _Builder(self, request_id, draft=True)
        bundle = prepare_birthday(
            draft, selected_indices=selected, event_ref=event.event_ref, due_at=event.order_due
        )
        requests = []
        for index in selected:
            prepared = birthday_prepared(draft, bundle, index)
            requests.append(
                PreviewRequest(
                    prepared.action,
                    prepared.cert,
                    inputs=draft.gateway.inputs,
                    authorizations=draft.gateway.authorizations,
                )
            )
        live = _Builder(self, request_id)
        actual = {}

        def prepare(index):
            if "bundle" not in actual:
                actual["bundle"] = prepare_birthday(
                    live,
                    selected_indices=selected,
                    event_ref=event.event_ref,
                    due_at=event.order_due,
                )
            prepared = birthday_prepared(live, actual["bundle"], index)
            actual.setdefault("approval_states", {})[prepared.action.node_id] = self.state
            return prepared

        preparers = {
            request.action.node_id: lambda index=index: prepare(index)
            for index, request in zip(selected, requests, strict=True)
        }
        display = self.workflow.show_plan(self.gateway, requests, preparers)
        plan_id = bundle["plan"].plan_id
        self._plans[plan_id] = {
            "display": display,
            "bundle": bundle,
            "actual": actual,
            "builder": live,
            "selected": selected,
            "event": event,
        }
        names = ["Lock birthday budget", "Unlock when due", "Order flowers", "Order cake"]
        data = {
            "plan_id": plan_id,
            "status": "draft",
            "canonical": True,
            "event": event_payload(event),
            "nodes": [
                {
                    "id": request.action.node_id,
                    "label": names[index],
                    "card": deepcopy(display.cards[request.action.node_id]),
                    "depends_on": bundle["plan"].nodes[index].depends_on,
                }
                for index, request in zip(selected, requests, strict=True)
            ],
        }
        self._requests[request_id] = deepcopy(data)
        return data

    def confirm_plan(self, plan_id, selected_node_ids):
        self._require_login()
        if plan_id in self._results:
            return deepcopy(self._results[plan_id])
        plan = self._plans[plan_id]
        refusal = self._scam_refusal(plan_id)
        if refusal is not None:
            return {**refusal, "plan_id": plan_id, "results": {}}
        displayed = [node.node_id for node in plan["bundle"]["plan"].nodes]
        if selected_node_ids != displayed:
            if (
                not selected_node_ids
                or len(set(selected_node_ids)) != len(selected_node_ids)
                or selected_node_ids != displayed[: len(selected_node_ids)]
            ):
                return {
                    "status": "selection_required",
                    "rule_ids": ["PLAN-SCOPE"],
                    "message": (
                        "Select each task and its prerequisites; "
                        "unchecked items receive no authority."
                    ),
                }
            return self._birthday_redisplay(plan_id, plan, list(range(len(selected_node_ids))), [])
        if not plan["actual"]:
            # The kernel's dependency hashes do not cover the calendar, so the event
            # and its derived dates are recomputed on current state before any signature.
            stale = self._birthday_event_check(plan_id, plan)
            if stale is not None:
                return stale
        confirmation = self.workflow.confirm_plan(
            self.gateway,
            plan["display"].display_id,
            selected_nodes=selected_node_ids,
            request_id=plan_id,
        )
        plan["confirmation"] = confirmation
        results = {key: self._result(value) for key, value in confirmation.results.items()}
        if confirmation.status == "redisplay" and not results and not plan["actual"]:
            return self._birthday_redisplay(plan_id, plan, plan["selected"], confirmation.rule_ids)
        success = (
            confirmation.status == "approved"
            and set(results) == set(selected_node_ids)
            and all(value["status"] in {"settled", "controlled"} for value in results.values())
        )
        result = {
            "status": "settled" if success else confirmation.status,
            "plan_id": plan_id,
            "results": results,
            "rule_ids": confirmation.rule_ids,
        }
        if success:
            self._results[plan_id] = result
        return deepcopy(result)

    def _birthday_redisplay(self, plan_id, plan, selected_indices, rule_ids):
        """Re-preview the same trusted event; a draft is never replaced by another event."""
        replacement = self.preview_birthday(
            selected_indices=selected_indices, event_ref=plan["event"].event_ref
        )
        if replacement["status"] == "draft":
            return {
                "status": "redisplay",
                "plan_id": replacement["plan_id"],
                "replacement": replacement,
                "results": {},
                "rule_ids": list(rule_ids),
            }
        if replacement["status"] == "outside_lock_month":
            return {
                "status": "outside_lock_month",
                "plan_id": plan_id,
                "event": replacement["event"],
                "results": {},
                "rule_ids": [],
            }
        return {
            "status": "redisplay",
            "plan_id": plan_id,
            "event_status": replacement["status"],
            "results": {},
            "rule_ids": list(rule_ids),
        }

    def _birthday_event_check(self, plan_id, plan):
        """Return a zero-signature outcome when the displayed event no longer holds."""
        shown = plan["event"]
        current = self.state.calendar.get(shown.event_ref)
        if (
            current is None
            or current.owner != self.device.principal
            or current.event_type != "birthday"
        ):
            return {
                "status": "redisplay",
                "plan_id": plan_id,
                "event_status": "event_not_eligible",
                "results": {},
                "rule_ids": [],
            }
        dates = derive_dates(current)
        if not lock_window_open(dates, self.state.now):
            return {
                "status": "outside_lock_month",
                "plan_id": plan_id,
                "event": event_payload(replace(dates, relation=shown.relation)),
                "results": {},
                "rule_ids": [],
            }
        if dates.order_due != shown.order_due or dates.contact_ref != shown.contact_ref:
            return self._birthday_redisplay(plan_id, plan, plan["selected"], [])
        return None

    def _registered_plan_ids(self):
        return list(
            dict.fromkeys(
                record["plan_id"]
                for record in self.gateway.audit.records()
                if record["event"] == "plan_registered"
            )
        )

    def _standing_plan(self, plan_id):
        """C1: a registered plan that is one standing transfer authorization (every node a
        scheduled transfer, as the transfer form and the chat's monthly transfer create)."""
        try:
            nodes = self.gateway.plan_snapshot(plan_id)["plan"]["nodes"]
        except (KeyError, ValueError, TypeError):
            return False
        return bool(nodes) and all(node.get("tool") == "schedule_transfer" for node in nodes)

    def stop(self, plan_id=None, *, keep_standing=False):
        """Interrupt one plan, or every registered plan and retire every unsigned card.

        ``keep_standing`` (C1, the chat's negated-transfer trigger) leaves established
        standing transfer authorizations running: they are revoked only through their own
        revocation card. An explicit Stop keeps its S1 meaning (every plan).
        """
        plans = [plan_id] if plan_id else self._registered_plan_ids()
        if keep_standing and plan_id is None:
            plans = [ref for ref in plans if not self._standing_plan(ref)]
        retired = 0
        if plan_id is None:
            retired = len(self._drafts) + len(self._pending)
            self._drafts.clear()
            self._pending.clear()
            self._closed.clear()
            self._plans = {key: value for key, value in self._plans.items() if key in plans}
        if not plans:
            return {
                "status": "interrupted" if retired else "no_active_plan",
                "results": {},
                "retired_drafts": retired,
            }
        results = {ref: self._result(self.gateway.interrupt(ref)) for ref in plans}
        if plan_id:
            return results[plan_id]
        return {"status": "interrupted", "results": results}

    def handoff(self, plan_id=None):
        plans = self._registered_plan_ids()
        plan_id = plan_id or (plans[-1] if plans else None)
        if plan_id is None:
            return {"status": "no_active_plan"}
        return self._result(self.gateway.handoff(plan_id))

    def resume(self, plan_id):
        self._require_login()
        # S6: a gated plan identity (even one no longer held as a draft) is checked; a
        # plan that never showed the questions resumes exactly as before.
        refusal = self._scam_refusal(plan_id)
        if refusal is not None:
            return {**refusal, "plan_id": plan_id}
        scope = self.gateway.resume_scope(plan_id)
        confirmation = self.device.confirm_input(
            intent_id=H(scope), slot="plan_resume", value=scope, now=self.state.now
        )
        return self._result(self.gateway.resume(plan_id, confirmation))

    def resume_scope(self, plan_id):
        return deepcopy(self.gateway.resume_scope(plan_id))

    def compensate(self, plan_id):
        proposed = self.gateway.request_compensation(plan_id)
        suggestions = proposed.details.get("suggestions", [])
        suggestion = next((item for item in suggestions if item["tool"] == "unlock_funds"), None)
        if suggestion is None:
            return {"status": "no_compensation", "suggestions": suggestions}
        receipt_ref = suggestion["receipt_ref"]
        amount = suggestion["amount"]
        result = self._preview_operation(
            lambda builder: prepare_funds(
                builder,
                "unlock_funds",
                amount,
                step=0,
                purpose={"original_receipt_ref": receipt_ref},
            ),
            request_id=self._id("compensation"),
        )
        if result.get("pending_id"):
            self._pending[result["pending_id"]]["compensation_plan_id"] = plan_id
        return result

    def next_due_target(self):
        """The earliest future scheduled run, so one jump never skips a run's window.

        Without such a run the former fixed operator target is used while it is still
        ahead of the simulated clock; otherwise there is nothing to advance to.
        """
        now = self.state.now
        due = [
            run.due_at
            for run in self.state.schedules.values()
            if run.status in {"pending", "due"} and run.due_at > now
        ]
        if due:
            return min(due)
        return OPERATOR_FALLBACK_TARGET if OPERATOR_FALLBACK_TARGET > now else None

    def advance_clock(self, target):
        if isinstance(target, int):
            target = add_seconds(self.state.now, target)
        results = self.gateway.advance_clock(target)
        return {
            "status": "advanced",
            "clock": self.state.now,
            "events": len(results),
            "results": [self._result(result) for result in results],
        }

    def display_vocabulary(self):
        """B1: synthetic display names the chat may resolve a product or merchant by.

        Read from the fixed display catalog by each catalog product's attribute signature
        and by merchant identifier, in both locales; the same names the pages show. Names
        only narrow trusted state; they never enter a card, an input or the audit.
        """
        from corridor.ui.messages import UI_MESSAGES

        def names(key):
            entry = UI_MESSAGES.get(key)
            return dict(entry) if entry else {}

        products = {
            product.product_id: names(display_names.product_key(product))
            for product in self.state.catalog.products
        }
        merchants = {
            ref: names(f"polish.merchant.{ref}")
            for ref in sorted({item.merchant_ref for item in self.state.subscriptions.values()})
        }
        return {
            "products": {ref: value for ref, value in products.items() if value},
            "merchants": {ref: value for ref, value in merchants.items() if value},
        }

    def _plan_snapshots(self, records):
        """``gateway.plan_snapshot`` of every registered plan, from one verified read of the
        audit log.

        ``plan_snapshot`` reads and verifies the whole log twice per plan, so a read grew with
        plans times records and a long conversation slowed to seconds (RW-9 item 4). This
        builds the same value from ``records`` and one checkpoint: each plan's records go to
        the gateway's own ``project_runtime``, which reads no others, and a plan it cannot
        project is left out as before.
        """
        held: dict[str, list] = {}
        for record in records:
            held.setdefault(record["plan_id"], []).append(record)
        checkpoint = self.gateway.audit.checkpoint()
        registered = dict.fromkeys(
            record["plan_id"] for record in records if record["event"] == "plan_registered"
        )
        snapshots = []
        for plan_id in registered:
            try:
                plan = self.gateway._plans[plan_id]
                runtime = project_runtime(plan, held[plan_id])
            except (KeyError, ValueError):
                continue
            snapshots.append({
                "plan": plan.model_dump(mode="json"),
                "ownership_epoch": plan.ownership_epoch,
                "nodes": {
                    node.node_id: {
                        "status": runtime.statuses[node.node_id],
                        "receipt_ref": runtime.receipt_refs.get(node.node_id),
                    }
                    for node in plan.nodes
                },
                "audit_checkpoint": dict(checkpoint),
                "blocked": plan_id in self.gateway._blocked_plans,
            })
        return snapshots

    def snapshot(self):
        audit = self.gateway.audit.records()
        plans = self._plan_snapshots(audit)
        # One copy of the state for the whole read: each ``self.state`` rebuilds it.
        state = self.state
        logged_in = (
            self.session_authorization is not None
            and self.session_authorization.expires_at > state.now
        )
        pending = []
        for key, value in self._pending.items():
            display = value["display"]
            card = display.cards[value["node_id"]] if value["mode"] == "draft" else display.card
            pending.append({
                "pending_id": key,
                "mode": value["mode"],
                "completed": key in self._results,
                "result": deepcopy(self._results.get(key)),
                "card": deepcopy(card),
                "canonical": True,
                "needs_mfa": card.get("tier") == "red",
                "verified": value.get("verified", False),
            })
        return {
            "logged_in": logged_in,
            "status": "ready" if logged_in else "login_required",
            "clock": state.now,
            "planner_mode": "scripted",
            "synthetic": True,
            "balances": {ref: fraction_string(available(state, ref)) for ref in state.accounts},
            "locked": self.device.principal in state.security.locked_at,
            "dispatch_count": self.gateway.bank.execute_count,
            "receipts": [item.model_dump(mode="json") for item in state.receipts.values()],
            "audit": audit,
            "audit_valid": self.gateway.verify_log(),
            "plans": plans,
            "schedules": [item.model_dump(mode="json") for item in state.schedules.values()],
            "positions": [item.model_dump(mode="json") for item in state.positions.values()],
            "cards": [
                item.model_dump(mode="json")
                for item in state.cards.values()
                if item.owner == self.device.principal
            ],
            "pending": pending,
        }
