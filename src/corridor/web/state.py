"""Server-side state of one web session: the browser tab's own controllers and pages.

One ``WebSession`` per (browser, tab), as Streamlit keeps one controller per browser
session (INV-11.3). Everything a Streamlit page kept in ``st.session_state`` lives here;
identifiers come from ``secrets`` only, never from the clock.

The session's product objects (the bank controller, the chat session, the wealth
controller) are held behind ``Product`` proxies: every method call outside a read-only
allow list first counts as a product call that may change state (``Effects``). A Write
handler reads the count before its action and after a refusal, so a refusal raised after
such a call is never answered as one that changed nothing (RW-5 item 2). The count lives
in a closure that ordinary code cannot reset, the proxies are bound to it when the session
is built, and the handler judges by the count only while that binding still holds
(``effects_bound``, RW-6 item 2).
"""

import asyncio
import functools
import secrets
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Literal

from corridor.web import compat

SENT_KEPT = 256

# --- the product-layer proxies -----------------------------------------------------------

# The product methods the web layer calls that do not change product state, each checked
# in its source (RW-5 item 2). Anything not listed here counts as a state change when it
# is called, private methods and attribute writes included: a missing entry fails closed.
CONTROLLER_READS: frozenset[str] = frozenset({
    # Builds a dict from the state: plan snapshots (a projection of the audit records), the
    # pending copies, balances, receipts, the audit records and ``verify_log`` (a read of
    # the chain). Writes nothing.
    "snapshot",
    # A copy of the gate record (``ScamGate.entry`` deep-copies) and its standing against
    # the current card (``refusal_reason`` reads the entry). Writes nothing.
    "scam_gate_status",
    # The stored proposal's verdict and a count of its dispatch rows in the audit records.
    "kernel_refusal",
    # ``not open_actions()``: reads the drafts, pendings, results and closed sets.
    "at_transaction_boundary",
    # The controller's own current card(s): reads the pendings, drafts and plans.
    "_authoritative_cards",
    # The plan ids among the ``plan_registered`` audit records.
    "_registered_plan_ids",
    # Level, cues and context from the pure ``scam_friction`` functions and
    # ``scam_contact`` (a read of the contact registry); returns a new tuple.
    "_scam_grade",
})
SESSION_READS: frozenset[str] = frozenset({
    # A deep copy of one stored reply.
    "response",
    # Sorted contact labels from ``trusted_slot_context`` (a projection of the state).
    "aa_roster",
    # ``controller.purchase_terms`` builds the preference on a draft copy: nothing is
    # signed or stored.
    "purchase_review_terms",
})
# The web layer calls the wealth controller only to start its session and to stop it.
WEALTH_READS: frozenset[str] = frozenset()


class Effects:
    """How many product calls that may change state a session has made.

    The count only grows: a Write action compares the count before and after its run, so
    nothing inside an action can clear what an earlier call recorded. The number itself is
    held in a closure made once, when the counter is created (RW-6 item 2): there is no
    field to write back, no setter, and the object takes no attribute writes or deletions;
    calling ``__init__`` again changes nothing.
    """

    __slots__ = ("_read", "_bump")

    def __new__(cls) -> "Effects":
        count = 0

        def read() -> int:
            return count

        def bump() -> None:
            nonlocal count
            count += 1

        self = super().__new__(cls)
        object.__setattr__(self, "_read", read)
        object.__setattr__(self, "_bump", bump)
        return self

    @property
    def count(self) -> int:
        return self._read()

    def touch(self) -> None:
        self._bump()

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError(f"Effects is read-only: {name}")

    def __delattr__(self, name: str) -> None:
        raise AttributeError(f"Effects is read-only: {name}")

    def __copy__(self) -> "Effects":
        raise TypeError("Effects is not copied")

    def __deepcopy__(self, memo: dict) -> "Effects":
        raise TypeError("Effects is not copied")

    def __reduce_ex__(self, protocol: object) -> Any:
        raise TypeError("Effects is not copied")

    def __repr__(self) -> str:
        return f"Effects({self.count})"


class Product:
    """A thin proxy around one product object; the session hands out nothing else.

    Field reads pass through. A method outside the object's read-only allow list is
    returned wrapped: calling it counts in ``Effects`` first, then runs it, so a call that
    raises halfway counts too. Writing or deleting an attribute counts as well. A method
    on the allow list is returned as it is.
    """

    __slots__ = ("_target", "_effects", "_readonly")

    def __init__(self, target: Any, effects: Effects, readonly: frozenset[str]) -> None:
        object.__setattr__(self, "_target", target)
        object.__setattr__(self, "_effects", effects)
        object.__setattr__(self, "_readonly", frozenset(readonly))

    def __getattr__(self, name: str) -> Any:
        value = getattr(self._target, name)
        if name in self._readonly or not callable(value):
            return value
        effects = self._effects

        @functools.wraps(value)
        def counted(*args: Any, **kwargs: Any) -> Any:
            effects.touch()
            return value(*args, **kwargs)

        return counted

    def __setattr__(self, name: str, value: Any) -> None:
        self._effects.touch()
        setattr(self._target, name, value)

    def __delattr__(self, name: str) -> None:
        self._effects.touch()
        delattr(self._target, name)

    def __repr__(self) -> str:
        return f"Product({self._target!r})"


def unwrap(value: Any) -> Any:
    """The product object behind a proxy (identity comparisons only), else ``value``."""
    return object.__getattribute__(value, "_target") if isinstance(value, Product) else value


# The session fields that hold product objects, each behind its own proxy.
PRODUCTS = ("controller", "session", "wealth")


def bound_effects(value: Any) -> "Effects | None":
    """The counter a proxy was built with; None for anything that is not a proxy."""
    if type(value) is not Product:
        return None
    return object.__getattribute__(value, "_effects")


def effects_bound(ws: Any, effects: Any) -> bool:
    """Whether ``effects`` is still the binding made when the session was built: an
    ``Effects``, the session's own counter, and the one each of its three proxies counts
    into (RW-6 item 2). A replaced counter, or a product object replaced or held without
    its proxy, is a binding the count cannot vouch for: the handler then answers a refusal
    as an unknown result (fail closed)."""
    return (
        type(effects) is Effects
        and getattr(ws, "effects", None) is effects
        and all(bound_effects(getattr(ws, name, None)) is effects for name in PRODUCTS)
    )


@dataclass
class Turn:
    id: str
    role: Literal["user", "assistant", "system"]
    rid: str | None
    hint: str | None
    external_files: int = 0
    shortcut: str | None = None
    marker: str | None = None


@dataclass
class DeviceTask:
    key: str
    source: Literal["chat", "wealth", "subscriptions", "plans", "compensation"]
    rkey: str | None
    rid: str | None
    identity: str | None
    kind: Literal["envelope", "local", "pending", "plan"]
    phase: Literal["idle", "in_call", "closed_failed", "closed_unknown"] = "idle"
    # A business task's place in the conversation: the reply it came from, if any, and the
    # transcript length when it was registered.
    origin: str | None = None
    since: int | None = None
    # Original unsigned chat purchase review, never replaced on a subsequent GET/refresh.
    review: dict | None = None


@dataclass
class Gesture:
    token: str
    op: Literal["confirm", "mfa_request", "mfa_verify"]
    step: str
    digest: str
    generation: str


@dataclass(eq=False)
class WebSession:
    browser_id: str
    tab_id: str
    generation: str
    secret: bytes = field(repr=False)
    view: str
    locale: str
    controller: Any
    wealth: Any
    session: Any
    pair: str
    revision: int = 0
    transcript: list[Turn] = field(default_factory=list)
    rkeys: dict[str, str] = field(default_factory=dict)
    rkey_of: dict[str, str] = field(default_factory=dict)
    current: dict[str, str | None] = field(default_factory=lambda: {"domestic": None})
    local: dict[str, dict] = field(default_factory=dict)
    tasks: dict[str, DeviceTask] = field(default_factory=dict)
    gestures: dict[str, Gesture] = field(default_factory=dict)
    gates: dict[str, tuple[str, str, str, int]] = field(default_factory=dict)
    gate_eval: dict[str, dict] = field(default_factory=dict)
    selection: dict[str, list[str]] = field(default_factory=dict)
    card_mismatch: dict[str, bool] = field(default_factory=dict)
    failures: dict[tuple[str, str], dict] = field(default_factory=dict)
    live_notes: dict[tuple[str, str], dict] = field(default_factory=dict)
    op_log: list[dict] = field(default_factory=list)
    live: Any = None
    scripted_client: Any = None
    live_toggle: bool = False
    pending_notices: list[dict] = field(default_factory=list)
    # Kernel views kept for the judge evidence after dispatch, and the display ids of
    # drafts whose confirmation record holds one.
    kernel_seen: dict[str, dict] = field(default_factory=dict)
    kernel_drafts: dict[str, str] = field(default_factory=dict)
    # Set when a state-changing call failed in an unexpected way: no token is issued again.
    untrusted: bool = False
    # Public mode's flags, set by the session table when the session is built or rebuilt
    # (``public.mark``); the view models read them and nothing else writes them.
    public: bool = False
    public_deployments: frozenset[str] | None = None
    # Counts every product call that may change state (see the module docstring).
    effects: Effects = field(default_factory=Effects)
    business: dict = field(default_factory=dict)
    demos: dict = field(default_factory=dict)
    requests: dict[str, str] = field(default_factory=dict)
    request_seq: int = 0
    sent: "OrderedDict[str, tuple[str, str]]" = field(default_factory=OrderedDict)
    safety: dict = field(default_factory=dict)
    busy: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def __post_init__(self) -> None:
        # Every product object a session holds is reached through a proxy that counts the
        # calls that may change state (RW-5 item 2); a session cannot be built without.
        # The three proxies are bound here to the one counter the session holds, and the
        # handler checks that binding before it trusts the count (RW-6 item 2).
        effects = self.effects
        if type(effects) is not Effects:
            raise TypeError("a web session counts its product calls in an Effects")
        self.controller = Product(unwrap(self.controller), effects, CONTROLLER_READS)
        self.session = Product(unwrap(self.session), effects, SESSION_READS)
        self.wealth = Product(unwrap(self.wealth), effects, WEALTH_READS)

    def request_id(self, kind: str, payload: object = None) -> str:
        """``app::request_id``: one id per (kind, payload); Stop clears them, not the count."""
        key = f"request:{kind}:{compat.H(payload)}"
        if key not in self.requests:
            self.requests[key] = compat.stable_id("UI-DEMO", self.request_seq, kind)
            self.request_seq += 1
        return self.requests[key]

    def forget_request(self, kind: str, payload: object = None) -> None:
        self.requests.pop(f"request:{kind}:{compat.H(payload)}", None)

    def turn_of(self, rkey: str) -> Turn:
        """The user turn that produced a reply (its words are the gate's text)."""
        rid = self.rkeys[rkey]
        for turn in reversed(self.transcript):
            if turn.rid == rid and turn.role == "user":
                return turn
        raise KeyError(rkey)


def new_web_session(view: str, locale: str, *, browser_id: str, tab_id: str) -> WebSession:
    controller = compat.DemoController(initial_state=compat.demo_state(showcase=True))
    wealth = compat.WealthController()
    pair = secrets.token_urlsafe(16)
    ws = WebSession(
        browser_id=browser_id,
        tab_id=tab_id,
        generation=secrets.token_urlsafe(9),
        secret=secrets.token_bytes(32),
        view=view,
        locale=locale,
        controller=controller,
        wealth=wealth,
        # The chat page shows model suggestions and sends the chosen one as a message.
        session=compat.DomesticSession(controller, wealth, suggest=True),
        pair=pair,
    )
    # The scripted planner the session was built with; live.on_new records the same.
    ws.scripted_client = controller.client
    # Last: a build that fails above leaves no inbox behind (RW-9 item 6).
    compat.OUTBOX.register(pair)
    return ws


def reserve_pairs(count: int) -> None:
    """Public mode: the outbox holds ``count`` pairings, so its own bound never drops a
    session in use (RW-9 item 1). Outside public mode nothing calls this."""
    compat.OUTBOX.reserve(count)


def retire_pair(ws: WebSession) -> None:
    """Every SMS of this pairing that still shows a code is marked expired."""
    for sms in compat.OUTBOX.inbox(ws.pair):
        if sms.status == "active":
            compat.OUTBOX.mark(ws.pair, sms.ref, "expired")


def forget_pair(ws: WebSession) -> None:
    """Drop this pairing's inbox from the outbox, after ``retire_pair`` (public mode only):
    a retired pairing then never counts against the outbox's bound of pairings, so churn
    cannot push a session still in use off the simulated phone (RW-8 item 4)."""
    compat.OUTBOX.forget(ws.pair)
