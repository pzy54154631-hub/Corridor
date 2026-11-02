"""The JSON shapes of web-contract/1 (§3, §4, §5) as TypedDicts, and a validator for tests.

Keys marked ``Judge`` exist only in the judge view: the validator reports them in a
customer-view body (INV-5.1: trimmed on the server, not hidden by the page). It also
rejects unknown keys, floats anywhere, wrong literals and malformed opaque keys, and it
checks that a response's top-level generation and revision match its state.
"""

import re
import types
from dataclasses import dataclass
from typing import (
    Annotated,
    Any,
    Literal,
    NotRequired,
    Required,
    TypedDict,
    Union,
    get_args,
    get_origin,
    get_type_hints,
    is_typeddict,
)


@dataclass(frozen=True)
class Pattern:
    regex: str


class _Judge:
    def __repr__(self) -> str:
        return "Judge"


JUDGE = _Judge()

View = Literal["customer", "judge"]
Locale = Literal["zh-CN", "en"]
ShortcutKey = Literal[
    "transfer", "billing", "wealth", "cards", "subscriptions", "birthday", "scam_drill"
]
TabName = Literal["accounts", "wealth", "plans", "operator"]
Generation = Annotated[str, Pattern(r"[A-Za-z0-9_-]{12}")]
TabId = Annotated[str, Pattern(r"[A-Za-z0-9_-]{32}")]
RKey = Annotated[str, Pattern(r"R[1-9][0-9]{0,5}")]
TKey = Annotated[str, Pattern(r"T(?:R[1-9][0-9]{0,5}|B[1-9][0-9]{0,5})")]
Choice = Annotated[str, Pattern(r"c(?:0|[1-9][0-9]{0,3})")]
NodeKey = Annotated[str, Pattern(r"n(?:0|[1-9][0-9]{0,2})")]
GateVersion = Annotated[str, Pattern(r"[A-Za-z0-9_-]{16}")]
Token = Annotated[str, Pattern(r"[A-Za-z0-9_-]{22}")]
CardVersion = Annotated[str, Pattern(r"[0-9a-f]{32}")]
Nonce = Annotated[str, Pattern(r"[0-9a-f]{32}")]
Hex16 = Annotated[str, Pattern(r"[0-9a-f]{16}")]
Hex64 = Annotated[str, Pattern(r"[0-9a-f]{64}")]
FactKey = Annotated[str, Pattern(r"[a-z_][a-z0-9_.]*")]
Ratio = Annotated[str, Pattern(r"(?:0|1)(?:\.[0-9]+)?")]
Obj = dict[str, Any]
Step = Literal[
    "review_purchase",
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
    "gate_later",
    "gate_plan",
    "closed_facts",
    "closed_challenge",
    "account_locked",
    "closed_failed",
    "closed_unknown",
    "withdrawn",
    "refused",
    "done",
    "unknown",
]
WHITELIST = frozenset({
    "review_purchase",
    "confirm",
    "confirm_intent",
    "mfa_request",
    "mfa_code",
    "final_confirm",
    "confirm_plan",
    "confirm_subset",
})
ErrorCode = Literal[
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
]


# --- §1.6 common types --------------------------------------------------------------------


class Notice(TypedDict):
    id: str
    level: Literal["info", "success", "warning", "error"]
    text: str


class TierVM(TypedDict):
    code: Literal["green", "yellow", "red", "black"]
    label: str
    glyph: Literal["circle", "diamond", "triangle", "square"]


class HashVM(TypedDict):
    algorithm: Literal["SHA-256(JCS)"]
    short: Hex16
    full: Hex64


class TaskHashVM(HashVM):
    status: Literal["pending", "signed", "not_yet"]
    label: str


# --- §4.1 AppState ---------------------------------------------------------------------


class Capabilities(TypedDict):
    tabs: list[Literal["accounts", "wealth", "plans"]]
    shortcuts: list[ShortcutKey]
    live: bool
    plan_select: bool
    compensate: bool
    aa: bool
    # The authorization lab's capability (CONTRACT-CHANGES R2-C1; judge view only).
    lab: bool


class SessionVM(TypedDict):
    logged_in: bool
    clock_text: str
    bank_name: str
    assistant_name: str
    phone_url: str


class LoginVM(TypedDict):
    title: str
    intro: str
    button: str
    caption: str


class SafetyVM(TypedDict):
    locked: bool
    text: str | None
    hint: str | None
    state: NotRequired[Annotated[Literal["Normal", "Watch", "Locked"], JUDGE]]
    reason: NotRequired[Annotated[str | None, JUDGE]]
    unlock_after_text: NotRequired[Annotated[str | None, JUDGE]]
    rule_ids: NotRequired[Annotated[list[str], JUDGE]]


class BadgeVM(TypedDict):
    kind: Literal["demo", "model"]
    text: str


class ShellVM(TypedDict):
    corner: str
    judge_strip: NotRequired[Annotated[str, JUDGE]]
    badge: BadgeVM
    safety: SafetyVM
    paused_banner: str | None
    notices: list[Notice]
    # Public mode only (1.1 public-mode addendum): the demo notice on every page.
    public_banner: NotRequired[str]
    public_disclosure: NotRequired[str]
    public_details_title: NotRequired[str]


# --- §4.3 cards -------------------------------------------------------------------------


class FactVM(TypedDict):
    key: FactKey
    label: str
    value: str


class NoteVM(TypedDict):
    kind: Literal[
        "suitability", "fraud", "recipient_context", "irreversible", "red", "schedule_scope"
    ]
    title: str | None
    lines: list[str]
    footer: str | None


class LinesVM(TypedDict):
    lines: list[str]


class CardTechnicalVM(TypedDict):
    card: Obj
    amount_raw: str | None
    contact_aliases: list[str]


class StructuredVM(TypedDict):
    label: str
    lines: list[str]


class CardVM(TypedDict):
    key: str
    title: str
    position: NotRequired[str]
    purpose: str
    amount_text: str
    tier: TierVM
    tier_reason: str
    facts: list[FactVM]
    notes: list[NoteVM]
    not_signed_label: str
    why: LinesVM
    technical: NotRequired[Annotated[CardTechnicalVM, JUDGE]]
    structured: NotRequired[Annotated[list[StructuredVM], JUDGE]]


# --- §4.4 the two questions ------------------------------------------------------------


class OptionVM(TypedDict):
    value: str
    label: str


class QuestionVM(TypedDict):
    name: Literal["verified", "guided"]
    label: str
    options: list[OptionVM]
    answer: str | None


class PauseVM(TypedDict):
    title: str
    copy: str
    retry_label: str
    self_report: str
    cancel_hint: str


class GateJudgeVM(TypedDict):
    note: str
    record: str
    preset_note: str | None


class GateVM(TypedDict):
    version: GateVersion
    title: str
    subtitle: str
    facts_title: str
    facts: list[str]
    cues: str | None
    questions_title: str
    questions: list[QuestionVM]
    decision: Literal["incomplete", "pause", "proceed", "stale"]
    locked: bool
    preset: bool
    interactive: bool
    pause: PauseVM | None
    notice: str | None
    self_report_note: str
    footer: str
    judge: NotRequired[Annotated[GateJudgeVM, JUDGE]]


# --- §4.5 results ----------------------------------------------------------------------


class BannerVM(TypedDict):
    tone: Literal["success", "info", "warning", "error"]
    text: str


class TotalVM(TypedDict):
    label: str
    value: str
    source: NotRequired[Annotated[str, JUDGE]]


class CategoryVM(TypedDict):
    label: str
    value: str
    ratio: Ratio


class AnomalyVM(TypedDict):
    title: str
    amount: str
    date: str
    reasons: str


class BillVM(TypedDict):
    totals: list[TotalVM]
    categories: list[CategoryVM]
    anomalies: list[AnomalyVM]
    empty_text: str | None


class SubscriptionRowVM(TypedDict):
    text: str
    cells: NotRequired[Annotated[list[str], JUDGE]]


class KeyLabelVM(TypedDict):
    key: str
    label: str


class SubscriptionsVM(TypedDict):
    snapshot_text: str
    renewals: list[str]
    rows: list[SubscriptionRowVM]
    empty_text: str | None
    footer: str
    cancel_choices: list[KeyLabelVM]
    cancel_version: NotRequired[CardVersion]


class TransactionsVM(TypedDict):
    columns: list[str]
    rows: list[list[str]]


class ResultVM(TypedDict):
    banner: BannerVM | None
    receipt_line: str | None
    amount_text: NotRequired[str]
    nodes: NotRequired[list["ResultVM"]]
    facts_lines: list[str]
    message_lines: list[str]
    why: LinesVM
    bill: NotRequired[BillVM]
    subscriptions: NotRequired[SubscriptionsVM]
    transactions: NotRequired[TransactionsVM]
    stop_receipt_lines: NotRequired[list[str]]
    rule_ids: NotRequired[Annotated[list[str], JUDGE]]
    receipt: NotRequired[Annotated[Obj | None, JUDGE]]
    audit_checkpoint: NotRequired[Annotated[Obj | None, JUDGE]]


class FailureVM(TypedDict):
    kind: Literal["failed", "unknown"]
    lines: list[str]
    cancel_hint: str


class DagNodeVM(TypedDict):
    key: str
    label: str
    status: str
    status_text: str
    tone: Literal["neutral", "wait", "run", "ok", "bad", "warn", "paused"]
    layer: int
    order: int


class DagVM(TypedDict):
    nodes: list[DagNodeVM]
    edges: list[tuple[str, str]]
    blocked_text: str | None
    details: NotRequired[Annotated[list[Obj], JUDGE]]


class ControlsVM(TypedDict):
    stop: str
    compensate: str | None
    handoff: str
    compensate_version: NotRequired[CardVersion]
    compensate_hint: NotRequired[str]


class TaskPanelVM(TypedDict):
    controls_in_details: NotRequired[bool]
    route_label: str
    tier: TierVM | None
    copy: str
    requested_hint: str | None
    dag: DagVM
    controls: ControlsVM


# --- §4.2 chat -------------------------------------------------------------------------


class ShortcutVM(TypedDict):
    key: ShortcutKey
    label: str


class TurnVM(TypedDict):
    id: str
    role: Literal["user", "assistant", "system"]
    rkey: NotRequired[RKey]
    text: str
    attachment_note: NotRequired[str]
    remark_note: NotRequired[str]


class RouteHintVM(TypedDict):
    target: Literal["accounts", "wealth", "plans", "operator", "fallback"]
    label: str
    note: str | None


class ChoiceVM(TypedDict):
    key: Choice
    label: str


class ClarifyVM(TypedDict):
    label: str
    choices: list[ChoiceVM]
    chosen: Choice | None
    review_label: str
    locked: bool
    locked_text: str | None
    error_text: str | None
    evidence_lines: NotRequired[Annotated[list[str], JUDGE]]


class ContactHintVM(TypedDict):
    """Under a transfer no contact matched: an explanation, never a choice."""

    lead: str
    contacts: list[str]
    retry: str


class PlanNodeVM(TypedDict):
    key: NodeKey
    label: str
    checked: bool


class PlanSelectVM(TypedDict):
    tkey: TKey
    rule_text: str
    nodes: list[PlanNodeVM]
    mode: Literal["none", "all", "subset", "invalid"]
    count: int
    total: int
    error_text: str | None


class AAVM(TypedDict):
    prompt: str
    needed: int
    choices: list[ChoiceVM]
    locked: NotRequired[bool]


class BirthdayChoiceVM(TypedDict):
    key: Choice
    label: str
    enabled: bool


class BirthdayVM(TypedDict):
    lines: list[str]
    choices: list[BirthdayChoiceVM]
    error_text: str | None


class SubscriptionSelectVM(TypedDict):
    choices: list[ChoiceVM]
    list_version: CardVersion
    locked: bool


class ScheduleCancelSelectVM(TypedDict):
    choices: list[ChoiceVM]
    list_version: CardVersion
    locked: bool


class SuggestionChoiceVM(TypedDict):
    key: Choice
    label: str
    state: Literal["offered", "adopted", "discarded", "expired"]
    state_text: str | None


class SuggestionSelectVM(TypedDict):
    """Model suggestions under a clarification: system-written questions, no authority."""

    note: str
    choices: list[SuggestionChoiceVM]
    list_version: CardVersion
    locked: bool
    evidence_lines: NotRequired[Annotated[list[str], JUDGE]]


class CancelVM(TypedDict):
    label: str
    hint: str


class ResponseVM(TypedDict):
    rkey: RKey
    current: bool
    status: str
    status_text: str
    copy_lines: list[str]
    tier: TierVM | None
    requested_tier: TierVM | None
    redisplay_notice: str | None
    cards: list[CardVM]
    plan_select: NotRequired[PlanSelectVM]
    clarify: NotRequired[ClarifyVM]
    contact_hint: NotRequired[ContactHintVM]
    aa: NotRequired[AAVM]
    birthday: NotRequired[BirthdayVM]
    subscription_select: NotRequired[SubscriptionSelectVM]
    schedule_cancel_select: NotRequired[ScheduleCancelSelectVM]
    suggestion_select: NotRequired[SuggestionSelectVM]
    gate: GateVM | None
    result: ResultVM | None
    failure: FailureVM | None
    live_notes: list[Notice]
    device_task: TKey | None
    route_hint: RouteHintVM | None
    fallback_shortcuts: bool
    cancel: CancelVM | None
    card_source: NotRequired[Annotated[Literal["authoritative", "envelope"], JUDGE]]
    card_mismatch: NotRequired[Annotated[str, JUDGE]]
    phrase_gate_note: NotRequired[Annotated[str, JUDGE]]
    parse_note: NotRequired[Annotated[list[str], JUDGE]]
    technical_url: NotRequired[Annotated[str, JUDGE]]


class GatedVM(TypedDict):
    rkey: RKey
    label: str


class ComposerVM(TypedDict):
    placeholder: str
    max_chars: Literal[4000]
    attach_label: str
    attach_note: str
    contacts_note: str
    disabled: bool


class ChatVM(TypedDict):
    shortcuts: list[ShortcutVM]
    drill_caption: str | None
    greeting: str | None
    turns: list[TurnVM]
    responses: dict[str, ResponseVM]
    current: RKey | None
    gated_elsewhere: list[GatedVM]
    task_panel: TaskPanelVM | None
    composer: ComposerVM


# --- §4.6 device -----------------------------------------------------------------------


class SelectionVM(TypedDict):
    count: int
    total: int
    text: str


class TextVM(TypedDict):
    text: str


class BackVM(TypedDict):
    rkey: RKey
    label: str


class MfaVM(TypedDict):
    sent_text: str
    method_text: str
    hint: str
    expired_text: str | None


class GestureVM(TypedDict):
    op: Literal["confirm", "mfa_request", "mfa_verify"]
    token: Token
    card_version: CardVersion
    label: str


class PurchaseReviewVM(TypedDict):
    title: str
    notice: str
    facts: list[FactVM]


class DeviceTaskVM(TypedDict):
    key: TKey
    source: Literal["chat", "wealth", "subscriptions", "plans", "compensation", "cards"]
    source_label: str
    rkey: NotRequired[RKey]
    current: bool
    cards: list[CardVM]
    purchase_review: NotRequired[PurchaseReviewVM]
    selection: NotRequired[SelectionVM]
    step: Step
    step_text: str
    gate_notice: NotRequired[TextVM]
    back: NotRequired[BackVM]
    mfa: NotRequired[MfaVM]
    gesture: GestureVM | None
    nodes_version: NotRequired[CardVersion]
    failure: NotRequired[FailureVM]
    result: NotRequired[ResultVM]
    cancel: CancelVM | None
    hash: NotRequired[Annotated[TaskHashVM, JUDGE]]


class SmsVM(TypedDict):
    url: str
    label: str
    open_label: str
    footer: str


class DeviceVM(TypedDict):
    title: str
    idle_text: str
    queue_text: str | None
    tasks: list[DeviceTaskVM]
    active: TKey | None
    sms: SmsVM


# --- §4.7 judge evidence ---------------------------------------------------------------


class PlannerVM(TypedDict):
    badge: str
    source: str
    counters: list[str]


class EvidenceResponseVM(TypedDict):
    rkey: RKey
    intent: str
    status: str
    rule_ids: list[str]
    rule_lines: list[str]
    audit_hash: HashVM


class KernelVM(TypedDict):
    outcome: Literal["Accept", "Escalate", "Reject"] | None
    tier: str | None
    rule_ids: list[str]
    rule_lines: list[str]
    note: str | None


class ConsistencyVM(TypedDict):
    display_card: HashVM
    proposal_card: HashVM | None
    same_hash: bool | None
    same_business_facts: bool | None
    action: HashVM | None
    cert: HashVM | None
    card_core: HashVM | None
    challenge: Literal["none", "issued", "verified"]
    not_yet_text: str | None
    mismatch_text: str | None


class ReceiptRowVM(TypedDict):
    id: str
    kind: str
    status: str
    time_text: str
    time_iso: NotRequired[str]


class DispatchVM(TypedDict):
    count: int
    receipts: list[ReceiptRowVM]


class AuditVM(TypedDict):
    count: int
    head: HashVM
    verified: bool
    note: str


class EvidenceSafetyVM(TypedDict):
    state: str
    reason: str | None


class IntentRowVM(TypedDict):
    intent: str
    status: str
    response: str
    utterance: str


class PlannerRowVM(TypedDict):
    rkey: RKey
    planner: str
    intent: str
    outcome: str | None
    tier: str | None
    rule_ids: list[str]


class EvidenceVM(TypedDict):
    open_by_default: list[Literal["kernel", "consistency", "dispatch"]]
    planner: PlannerVM
    response: NotRequired[EvidenceResponseVM]
    kernel: NotRequired[KernelVM]
    consistency: NotRequired[ConsistencyVM]
    dispatch: DispatchVM
    audit: AuditVM
    safety: EvidenceSafetyVM
    gate_record: NotRequired[str]
    intent_log: list[IntentRowVM]
    intent_log_note: str
    planner_log: list[PlannerRowVM]
    footer: str


class AppState(TypedDict):
    schema: Literal[1]
    contract: Literal["web-contract/1.1"]
    generation: Generation
    revision: int
    view: View
    locale: Locale
    capabilities: Capabilities
    session: SessionVM
    shell: ShellVM
    login: NotRequired[LoginVM]
    chat: ChatVM
    device: DeviceVM
    evidence: NotRequired[Annotated[EvidenceVM, JUDGE]]


# --- §5 tabs, live, phone, technical evidence -------------------------------------------


class BalanceVM(TypedDict):
    label: str
    amount: str


class CardRowVM(TypedDict):
    label: str
    status_text: str


class ReceiptVM(TypedDict):
    title: str
    amount: str
    time: str
    status: str


class SubQueryVM(TypedDict):
    label: str
    task: NotRequired[TKey]


class AccountsVM(TypedDict):
    card_controls: NotRequired[Obj]
    balances: list[BalanceVM]
    cards: list[CardRowVM]
    receipts: list[ReceiptVM]
    subscriptions: SubscriptionsVM | None
    sub_query: SubQueryVM | None
    billing: BillVM | None
    empty_text: str | None


class RefusalVM(TypedDict):
    title: str
    lead: str
    card: CardVM
    reasons: list[str]
    dispatches_text: str
    isolation: str
    outcome: NotRequired[Annotated[str, JUDGE]]
    rule_ids: NotRequired[Annotated[list[str], JUDGE]]


class ObjectiveVM(TypedDict):
    value: str
    options: list[OptionVM]


class CellVM(TypedDict):
    label: str
    value: str


class ComparisonRowVM(TypedDict):
    key: str
    name: str
    cells: list[CellVM]
    chosen: bool


class ExclusionVM(TypedDict):
    name: str
    reasons: str


class ComparisonVM(TypedDict):
    banner: str
    rows: list[ComparisonRowVM]
    exclusions: list[ExclusionVM]
    candidates: list[KeyLabelVM]
    candidates_version: NotRequired[CardVersion]
    bic: NotRequired[Annotated[Obj, JUDGE]]


class PurchaseVM(TypedDict):
    task: NotRequired[TKey]
    card: CardVM
    refusal: NotRequired[RefusalVM]


class PositionVM(TypedDict):
    key: str
    label: str
    redeemable: bool
    caption: str
    from_text: str


class RedemptionVM(TypedDict):
    task: NotRequired[TKey]
    card: CardVM


class WealthVM(TypedDict):
    questionnaire: NotRequired[Obj | None]
    intro: str
    scope_note: str
    objective: ObjectiveVM
    comparison: ComparisonVM | None
    purchase: PurchaseVM | None
    positions: list[PositionVM]
    positions_version: CardVersion
    redemption: RedemptionVM | None
    results: list[ResultVM]
    r4: RefusalVM | None
    live_scope_note: NotRequired[str]


class PlanVM(TypedDict):
    key: str
    title: str
    dag: DagVM
    blocked_text: str | None


class PlanDraftVM(TypedDict):
    task: TKey
    select: PlanSelectVM


class PlansVM(TypedDict):
    plans: list[PlanVM]
    draft: PlanDraftVM | None
    empty_text: str


class CompensationVM(TypedDict):
    label: str
    task: NotRequired[TKey]
    result: NotRequired[ResultVM]
    compensate_version: NotRequired[CardVersion]
    compensate_hint: NotRequired[str]


class OperatorVM(TypedDict):
    intro: str
    result: ResultVM | None
    snapshot_lines: list[str]
    snapshot: NotRequired[Annotated[Obj, JUDGE]]
    compensation: CompensationVM | None
    note: str


TAB_VMS: dict[str, Any] = {
    "accounts": AccountsVM,
    "wealth": WealthVM,
    "plans": PlansVM,
    "operator": OperatorVM,
}


class TabPayload(TypedDict):
    name: TabName
    revision: int
    vm: Obj


class DeploymentVM(TypedDict):
    id: str
    label: str
    credential_text: NotRequired[Annotated[str, JUDGE]]


class UnlockVM(TypedDict):
    disabled: bool
    label: str
    button: str
    result_text: str | None


class ToggleVM(TypedDict):
    on: bool
    disabled: bool
    label: str


class LiveVM(TypedDict):
    enabled: bool
    config_invalid: bool
    title: str
    intro: str
    off_text: str | None
    deployments: list[DeploymentVM]
    selected: str | None
    price: NotRequired[Annotated[str, JUDGE]]
    unlock: UnlockVM
    revoked_text: str | None
    toggle: ToggleVM
    reason_text: str | None
    boundary: bool
    boundary_text: str | None
    caps_text: str | None
    kernel_note: str | None
    no_secret: str | None
    badge: BadgeVM
    scope_note: str | None
    counters: NotRequired[Annotated[list[str], JUDGE]]
    notice: str | None


class SmsMessageVM(TypedDict):
    text: str
    status: Literal["active", "used", "expired"]
    latest: bool
    status_text: NotRequired[str]


class PhonePayload(TypedDict):
    ok: Literal[True]
    version: str
    title: str
    status_bar: str
    corner: str
    messages: list[SmsMessageVM]
    empty: str | None
    unpaired: str | None
    footer: str
    disclosure: NotRequired[Annotated[str, JUDGE]]
    # Public mode only (1.1 public-mode addendum): the demo notice on the phone page.
    banner: NotRequired[str]
    public_disclosure: NotRequired[str]
    public_details_title: NotRequired[str]


class EvidenceTechnical(TypedDict):
    envelope: Obj
    cards: list[Obj]
    kernel: Obj | None
    gate_record: Obj | None


# --- §1.3 response envelopes -----------------------------------------------------------


ResultStatus = Literal[
    "accepted",
    "logged_in",
    "complete",
    "ready",
    "login_required",
    "settled",
    "controlled",
    "awaiting_approval",
    "waiting_mfa",
    "mfa_verified",
    "redisplay",
    "selection_required",
    "intent_confirmation_required",
    "rejected",
    "refused",
    "failed",
    "blocked",
    "withdrawn",
    "interrupted",
    "pending",
    "running",
    "waiting_settlement",
    "succeeded",
    "completed",
    "draft",
]


class StatusVM(TypedDict):
    status: ResultStatus


class KdfVM(TypedDict):
    """Public mode only (1.1 public-mode addendum): the verifier's derivation parameters
    sent with a challenge, so the browser proves the configured verifier, not the code."""

    algorithm: Literal["pbkdf2-sha256"]
    iterations: int
    salt: Annotated[str, Pattern(r"[0-9a-f]{32}")]


class OkState(TypedDict, total=False):
    ok: Required[Literal[True]]
    generation: Required[Generation]
    revision: Required[int]
    state: Required[AppState]
    tab: TabId | TabPayload
    rkey: RKey
    result: StatusVM
    nonce: Nonce
    kdf: KdfVM


class OkLiveResult(TypedDict):
    ok: Literal[True]
    generation: Generation
    revision: int
    state: NotRequired[AppState]
    result: Literal["accepted", "invalid", "locked", "locked_all", "stale", "unavailable"]
    live: LiveVM


class OkStateLive(TypedDict):
    """``/api/live/select`` and ``/api/live/toggle``: the state and the panel, no result."""

    ok: Literal[True]
    generation: Generation
    revision: int
    state: AppState
    live: LiveVM


class OkTab(TypedDict):
    ok: Literal[True]
    generation: Generation
    revision: int
    tab: TabPayload


class OkLive(TypedDict):
    ok: Literal[True]
    generation: Generation
    revision: int
    live: LiveVM


class OkI18n(TypedDict):
    ok: Literal[True]
    generation: Generation
    revision: int
    locale: Locale
    view: View
    strings: dict[str, str]


class OkEvidence(TypedDict):
    ok: Literal[True]
    generation: Generation
    revision: int
    available: bool
    technical: NotRequired[Annotated[EvidenceTechnical, JUDGE]]


class ErrorVM(TypedDict):
    code: ErrorCode
    message: str
    retry: bool


class Err(TypedDict):
    ok: Literal[False]
    error: ErrorVM
    generation: NotRequired[Generation]
    revision: NotRequired[int]
    state: NotRequired[AppState]


# --- validator --------------------------------------------------------------------------


class ContractError(ValueError):
    """A body that does not match web-contract/1; ``problems`` lists every finding."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems[:20]))
        self.problems = problems


def _unwrap(hint: Any) -> tuple[Any, tuple[Any, ...]]:
    """Strip NotRequired / Required / Annotated; return the base type and its metadata."""
    meta: tuple[Any, ...] = ()
    while True:
        origin = get_origin(hint)
        if origin in (NotRequired, Required):
            hint = get_args(hint)[0]
        elif origin is Annotated:
            base, *extra = get_args(hint)
            meta += tuple(extra)
            hint = base
        else:
            return hint, meta


def _json(value: Any, path: str, found: list[str]) -> None:
    if isinstance(value, float):
        found.append(f"{path}: float is not allowed")
    elif isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                found.append(f"{path}: non-string key")
            _json(item, f"{path}.{key}", found)
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _json(item, f"{path}[{index}]", found)
    elif value is not None and not isinstance(value, (str, int, bool)):
        found.append(f"{path}: {type(value).__name__} is not JSON")


def _check(value: Any, hint: Any, path: str, view: str, found: list[str]) -> None:
    hint, meta = _unwrap(hint)
    for item in meta:
        if isinstance(item, Pattern) and not (
            isinstance(value, str) and re.fullmatch(item.regex, value)
        ):
            found.append(f"{path}: {value!r} does not match {item.regex}")
            return
    origin = get_origin(hint)
    if hint is Any:
        _json(value, path, found)
    elif is_typeddict(hint):
        _typeddict(value, hint, path, view, found)
    elif origin is Literal:
        if value not in get_args(hint) or isinstance(value, bool) != any(
            isinstance(option, bool) for option in get_args(hint)
        ):
            found.append(f"{path}: {value!r} not in {get_args(hint)}")
    elif origin in (Union, types.UnionType):
        for option in get_args(hint):
            trial: list[str] = []
            _check(value, option, path, view, trial)
            if not trial:
                return
        found.append(f"{path}: {value!r} matches none of {get_args(hint)}")
    elif origin is list:
        if not isinstance(value, list):
            found.append(f"{path}: expected a list")
            return
        (item_hint,) = get_args(hint)
        for index, item in enumerate(value):
            _check(item, item_hint, f"{path}[{index}]", view, found)
    elif origin is dict:
        if not isinstance(value, dict):
            found.append(f"{path}: expected an object")
            return
        key_hint, item_hint = get_args(hint)
        for key, item in value.items():
            _check(key, key_hint, f"{path}<key>", view, found)
            _check(item, item_hint, f"{path}.{key}", view, found)
    elif origin is tuple:
        items = get_args(hint)
        if not isinstance(value, (list, tuple)) or len(value) != len(items):
            found.append(f"{path}: expected {len(items)} items")
            return
        for index, (item, item_hint) in enumerate(zip(value, items, strict=True)):
            _check(item, item_hint, f"{path}[{index}]", view, found)
    elif hint is type(None):
        if value is not None:
            found.append(f"{path}: expected null")
    elif hint is bool:
        if not isinstance(value, bool):
            found.append(f"{path}: expected a boolean")
    elif hint is int:
        if isinstance(value, bool) or not isinstance(value, int):
            found.append(f"{path}: expected an integer")
    elif hint is str:
        if not isinstance(value, str):
            found.append(f"{path}: expected a string")
    else:
        found.append(f"{path}: unsupported type {hint!r}")


def _typeddict(value: Any, td: Any, path: str, view: str, found: list[str]) -> None:
    if not isinstance(value, dict):
        found.append(f"{path}: expected an object")
        return
    hints = get_type_hints(td, include_extras=True)
    for key in value:
        if key not in hints:
            found.append(f"{path}.{key}: not in the contract")
    for key, hint in hints.items():
        _, meta = _unwrap(hint)
        if key not in value:
            if key in td.__required_keys__:
                found.append(f"{path}.{key}: missing")
            continue
        if JUDGE in meta and view == "customer":
            found.append(f"{path}.{key}: judge-only key in the customer view")
            continue
        _check(value[key], hint, f"{path}.{key}", view, found)
    if td is TabPayload and isinstance(value.get("vm"), dict) and value.get("name") in TAB_VMS:
        _typeddict(value["vm"], TAB_VMS[value["name"]], f"{path}.vm", view, found)
    if td in (HashVM, TaskHashVM) and isinstance(value.get("full"), str):
        if value.get("short") != value["full"][:16]:
            found.append(f"{path}.short: not the first 16 digits of full")


def _state_rules(state: Any, path: str, found: list[str]) -> None:
    """Cross-field rules of §4 that a type alone cannot state."""
    if not isinstance(state, dict):
        return
    chat = state.get("chat") or {}
    responses = chat.get("responses") or {}
    keys = {turn.get("rkey") for turn in chat.get("turns") or [] if isinstance(turn, dict)}
    for rkey, response in responses.items():
        if isinstance(response, dict) and response.get("rkey") != rkey:
            found.append(f"{path}.chat.responses.{rkey}: rkey differs from its key")
        if rkey not in keys:
            found.append(f"{path}.chat.responses.{rkey}: not a reply of the listed turns")
    if chat.get("current") is not None and chat["current"] not in responses:
        found.append(f"{path}.chat.current: not among the responses")
    login = "login" in state
    if login and (chat.get("turns") or (state.get("device") or {}).get("tasks")):
        found.append(f"{path}: a login page carries no turns and no device tasks")
    for index, task in enumerate((state.get("device") or {}).get("tasks") or []):
        if not isinstance(task, dict):
            continue
        if task.get("gesture") is not None and task.get("step") not in WHITELIST:
            found.append(f"{path}.device.tasks[{index}]: a gesture outside the whitelist")


def _shape(body: dict[str, Any]) -> Any:
    if body.get("ok") is False:
        return Err
    if "state" in body and "live" in body:
        return OkLiveResult if "result" in body else OkStateLive
    if "state" in body:
        return OkState
    if "strings" in body:
        return OkI18n
    if "available" in body:
        return OkEvidence
    if "live" in body:
        return OkLive
    if "tab" in body:
        return OkTab
    if "messages" in body:
        return PhonePayload
    return None


def problems(body: Any, *, view: str | None = None) -> list[str]:
    """Every contract finding for one response body (empty when it conforms).

    The view for judge-only keys comes from the body (``state.view`` or ``view``); pass
    ``view`` for bodies that do not carry it (tabs, phone, live, evidence).
    """
    found: list[str] = []
    if not isinstance(body, dict):
        return ["body: expected an object"]
    _json(body, "body", found)
    shape = _shape(body)
    if shape is None:
        return [*found, "body: no contract shape matches"]
    state = body.get("state")
    seen = view or (state or {}).get("view") or body.get("view") or "judge"
    _typeddict(body, shape, "body", seen, found)
    if isinstance(state, dict):
        for key in ("generation", "revision"):
            if key in body and body[key] != state.get(key):
                found.append(f"body.{key}: differs from state.{key}")
        _state_rules(state, "body.state", found)
    return found


def validate_response(body: Any, *, view: str | None = None) -> None:
    """Raise ``ContractError`` unless the body conforms to web-contract/1."""
    found = problems(body, view=view)
    if found:
        raise ContractError(found)


def validate_state(state: Any) -> None:
    """Raise ``ContractError`` unless one AppState conforms (its own view decides)."""
    found: list[str] = []
    _json(state, "state", found)
    seen = state.get("view", "judge") if isinstance(state, dict) else "judge"
    _typeddict(state, AppState, "state", seen, found)
    _state_rules(state, "state", found)
    if found:
        raise ContractError(found)


validate = validate_response
check_response = validate_response
