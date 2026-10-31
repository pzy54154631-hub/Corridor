"""The live-model switch on the domestic banking page: badges, an expander, two caps.

Everything here is a projection of ``LiveSession`` snapshots and of the outcomes of the
calls one operation made (an operation is one gesture: a sent request, a confirmation).
A note or a failure line is always about that operation's own calls, never a sticky
"last call". The page never holds a key or a code: the access field is a small browser
component that hashes the typed code with the session's single-use challenge and sends
only that proof (``live_access/``). When the switch is off and no live client was ever
used this module renders the badge only and changes nothing in session state; when it
goes off while a live client exists, new external calls stop at once and the scripted
planner takes over at the transaction boundary.
"""

from contextvars import ContextVar
from fractions import Fraction
from html import escape
from pathlib import Path
from typing import Any

import streamlit as st

from corridor.agent.domestic_suggest import suggestion_step
from corridor.agent.llm import BudgetExceeded, LLMClient, LLMConfigurationError, LLMOutputError
from corridor.schemas.money import display_amount
from corridor.ui import view_mode
from corridor.ui.live_config import LiveConfig
from corridor.ui.live_runtime import PROCESS, RESERVE_INPUT_TOKENS, JournalClient, LiveSession
from corridor.ui.messages import tr

SESSION_KEY = "live_session"
SCRIPTED_KEY = "live_scripted_client"
TOGGLE_KEY = "live_mode"
NOTICE_KEY = "live_exhausted_notice"
# The access field's value: {"nonce", "proof"}, single use; never a code.
PROOF_KEY = "live_access_proof"
COMPONENT_DIR = Path(__file__).with_name("live_access")
# Plain wording for every way one live call can fail; anything else uses the generic one.
FAILURE_REASONS = (
    "output-rejected",
    "live-error",
    "budget-rejected",
    "pricing-rejected",
    "context-rejected",
    "record-exists",
    "replay-corrupt",
    "replay-miss",
    "agent-rejected",
    "ledger-unavailable",
    "ledger-unreconciled",
    "live-revoked",
)
_BADGE_STYLE = (
    "display:inline-block;border-radius:14px;padding:3px 12px;font-size:12px;"
    "font-weight:600;margin:2px 0 8px;"
)
# Per script run: the judge counters' placeholder and where this run's calls start.
_judge_slot: ContextVar[Any] = ContextVar("corridor_live_judge_slot", default=None)
_run_mark: ContextVar[int | None] = ContextVar("corridor_live_run_mark", default=None)


def _is_live(controller) -> bool:
    return isinstance(getattr(controller, "client", None), JournalClient)


def _usable(controller) -> bool:
    """The controller's live client may still call: not switched off, revoked or replaced."""
    live = st.session_state.get(SESSION_KEY)
    return (
        _is_live(controller)
        and isinstance(live, LiveSession)
        and live.is_current(getattr(controller, "client", None))
    )


def live_label(controller) -> str | None:
    """The banking controller's live deployment, or None while it plans with scripts."""
    return controller.client.deployment.label if _is_live(controller) else None


def live_revoked(controller) -> bool:
    """A live client is still attached (an action is open) but may no longer call."""
    return _is_live(controller) and not _usable(controller)


def _scope(controller):
    """What the badge describes: an open demo preset's own scripted controller, the
    scripted wealth advisor on its page, otherwise the banking chat planner."""
    if st.session_state.get("show_demo"):
        demo = st.session_state.get("demo_scenario")
        if isinstance(demo, dict) and "controller" in demo:
            return demo["controller"], "preset"
    page = st.session_state.get("page")
    if page == "wealth" and st.session_state.get("conversation_route") != "wealth":
        wealth = st.session_state.get("wealth_controller")
        if wealth is not None:
            return wealth, "wealth"
    return controller, "bank"


def _badge(controller, info: dict | None) -> None:
    shown, scope = _scope(controller)
    if _is_live(shown):
        exhausted = info is not None and info["reason"] == "budget_exhausted"
        key = (
            "live.badge.live_revoked"
            if not _usable(shown)
            else "live.badge.live_exhausted"
            if exhausted
            else "live.badge.live"
        )
        text = tr(key, deployment=shown.client.deployment.label)
        style = _BADGE_STYLE + "background:#e8f3ea;color:#1f5e2e;border:1px solid #9fcfab;"
        kind = "live-badge-model"
    else:
        text = tr("live.badge.scripted")
        style = _BADGE_STYLE + "background:#f1eef6;color:#4a3d63;border:1px solid #d6cbe6;"
        kind = "live-badge-demo"
    st.markdown(
        f'<div class="live-badge {kind}" style="{style}">{escape(text)}</div>',
        unsafe_allow_html=True,
    )
    if scope != "bank" and _is_live(controller):
        # The badge above describes this page's own planner; say where the live one is.
        st.caption(tr("live.scope." + scope, deployment=controller.client.deployment.label))


def _use_scripted(controller, live: LiveSession) -> None:
    """Hand the controller and its chat session back to the scripted planner (at the
    transaction boundary only: the controller refuses a change during a transaction)."""
    session = st.session_state.get("domestic_session")
    scripted = st.session_state.get(SCRIPTED_KEY)
    if not isinstance(scripted, LLMClient) or scripted.mode != "scripted":
        scripted = LLMClient(
            mode="scripted", env_file=None, context_guard=controller.gateway.vault.ensure_safe
        )
    controller.set_client(scripted)
    if session is not None and session.controller is controller:
        session.client = scripted
    live.release()


def _apply(controller, live: LiveSession, enabled: bool, info: dict, boundary: bool) -> None:
    """Switch the controller and its chat session between the scripted and live clients."""
    session = st.session_state.get("domestic_session")
    if enabled and info["available"] and boundary:
        try:
            client = live.client(controller, context_guard=controller.gateway.vault.ensure_safe)
        except (BudgetExceeded, LLMConfigurationError, OSError):
            # The journal directory is unwritable, the access lapsed or the client refused
            # its configuration. This is the transaction boundary, so a live client that
            # can no longer be rebuilt is replaced by the scripted planner here; the page
            # says which of the two actually happened.
            returned = _is_live(controller)
            if returned:
                _use_scripted(controller, live)
            st.error(tr("live.client_failed_returned" if returned else "live.client_failed"))
            return
        if client is not controller.client:
            if not _is_live(controller):
                st.session_state[SCRIPTED_KEY] = controller.client
            controller.set_client(client)
            if session is not None and session.controller is controller:
                session.client = client
    elif _is_live(controller) and boundary and not (enabled and info["available"]):
        _use_scripted(controller, live)


def _reason_line(controller, info: dict, boundary: bool, judge: bool) -> str:
    reason = info["reason"]
    if _is_live(controller) and not _usable(controller):
        return tr("live.reason.revoked_pending")
    if reason in {
        "disabled",
        "missing_journal",
        "inherited_env",
        "missing_credentials",
        "weak_code",
    }:
        if not judge:
            return tr("live.customer.reason.unavailable")
        if reason == "inherited_env":
            return tr("live.reason.inherited_env", names=", ".join(info["inherited_env"]))
        return tr("live.reason." + reason)
    if reason == "ready" and _is_live(controller):
        return tr("live.reason.on", deployment=controller.client.deployment.label)
    if reason == "budget_exhausted" and _is_live(controller) and not boundary:
        return tr("live.reason.budget_pending")
    return tr("live.reason." + reason)


def _access_component(*, nonce: str, label: str, button: str, disabled: bool, key: str):
    """The access field: a password box whose code is hashed in the browser.

    Returns the last value the field sent: ``{"nonce", "proof"}`` (the proof is
    HMAC-SHA256 keyed by the code over the single-use challenge), or None. The code itself
    is never sent to the server and so never enters the Streamlit session.
    """
    from streamlit.components.v1 import declare_component

    component = declare_component("corridor_live_access", path=str(COMPONENT_DIR))
    return component(
        nonce=nonce, label=label, button=button, disabled=disabled, key=key, default=None
    )


def _render_off(controller, invalid: bool) -> None:
    """The switch is off or the configuration no longer parses.

    A session that had live access stops new external calls at once (its grant is
    revoked and its client refuses), and the scripted planner takes over at the
    transaction boundary; until then the badge and a caption say that new requests are
    not sent to the model. Without such a session nothing in session state changes.
    """
    live = st.session_state.get(SESSION_KEY)
    if isinstance(live, LiveSession):
        live.refresh(LiveConfig())
        if _is_live(controller) and controller.at_transaction_boundary() and not live.in_flight:
            _use_scripted(controller, live)
    if invalid:
        st.error(tr("live.config_invalid_live" if _is_live(controller) else "live.config_invalid"))
    _badge(controller, None)
    if _is_live(controller):
        st.caption(tr("live.reason.revoked_pending"))
    elif view_mode.is_judge():
        st.caption(tr("live.off_hint"))


def render_live_controls(controller) -> LiveSession | None:
    """Render the badge and, with the switch on, the access and deployment controls."""
    _judge_slot.set(None)
    _run_mark.set(None)
    try:
        config = LiveConfig.from_environ()
    except LLMConfigurationError:
        config = None
    if config is None or not config.enabled:
        _render_off(controller, invalid=config is None)
        return None
    live = st.session_state.get(SESSION_KEY)
    if not isinstance(live, LiveSession):
        live = LiveSession(config, environ=PROCESS)
        st.session_state[SESSION_KEY] = live
    else:
        live.refresh(config)
    _run_mark.set(live.mark())
    judge = view_mode.is_judge()
    boundary = controller.at_transaction_boundary() and not live.in_flight
    # The access field's proof (never a code): checked once against the current challenge,
    # which is then used up, so a stale value left in the field's state never counts.
    access = None
    proof = st.session_state.get(PROOF_KEY)
    if boundary and isinstance(proof, dict) and proof.get("nonce") == live.challenge():
        access = live.authorize_proof(proof.get("nonce"), proof.get("proof"))
    if proof is not None:
        # C3: a proof leaves the session once read (used or stale); the field itself sends
        # a null value when the next challenge arrives, so its widget state clears too.
        del st.session_state[PROOF_KEY]
    info = live.snapshot()
    # Live access lost while the switch was on (a cap reached, above all): turn it off
    # before the widget is drawn; the scripted planner takes over at the transaction
    # boundary, and a cap says so once.
    if st.session_state.get(TOGGLE_KEY) and not info["available"] and boundary:
        st.session_state[TOGGLE_KEY] = False
        if info["reason"] == "budget_exhausted":
            st.session_state[NOTICE_KEY] = (
                "calls" if info["calls_remaining"] == 0 and info["calls"] > 0 else "spend"
            )
    # The switch's value from the last interaction is already in session state: apply it
    # before anything is drawn, so the title, the reason line, the badge and the judge
    # strip all describe the planner that answers in this run.
    _apply(controller, live, bool(st.session_state.get(TOGGLE_KEY)), info, boundary)
    info = live.snapshot()
    title = (
        tr("live.expander_on", deployment=controller.client.deployment.label)
        if _is_live(controller)
        else tr("live.expander")
    )
    with st.expander(title, expanded=False):
        st.caption(tr("live.intro" if judge else "live.customer.intro"))
        options = [item["id"] for item in info["deployments"]]
        labels = {
            item["id"]: (
                f"{item['label']} · {tr('live.credential.' + item['credential'])}"
                if judge
                else item["label"]
            )
            for item in info["deployments"]
        }
        chosen = st.selectbox(
            tr("live.deployment"),
            options,
            index=options.index(info["deployment"]),
            format_func=labels.__getitem__,
            key="live_deployment",
            disabled=not boundary or _is_live(controller),
        )
        if chosen != info["deployment"] and boundary and not _is_live(controller):
            live.select(chosen)
            info = live.snapshot()
        current = next(item for item in info["deployments"] if item["id"] == info["deployment"])
        if judge:
            st.caption(
                tr(
                    "live.price",
                    model=current["model"],
                    price_in=current["price_in"],
                    price_out=current["price_out"],
                    currency=current["currency"],
                    note=tr(current["price_note"]),
                )
            )
        locked = info["reason"] in {"locked", "locked_all"}
        _access_component(
            nonce=live.challenge(),
            label=tr("live.access_code"),
            button=tr("live.unlock"),
            disabled=not boundary or locked,
            key=PROOF_KEY,
        )
        if access == "accepted":
            st.success(tr("live.unlocked"))
        elif access == "invalid":
            st.warning(tr("live.denied"))
        elif access == "locked":
            st.warning(tr("live.locked_out"))
        elif access == "locked_all":
            st.warning(tr("live.locked_out_all"))
        elif access is not None and access != "stale":
            st.warning(
                tr("live.unlock_unavailable" if judge else "live.customer.reason.unavailable")
            )
        if info["revoked"] == "credentials" and not info["authorized"]:
            st.warning(tr("live.revoked.credentials"))
        st.toggle(tr("live.toggle"), key=TOGGLE_KEY, disabled=not info["available"] or not boundary)
        st.caption(_reason_line(controller, info, boundary, judge))
        if not boundary:
            st.caption(tr("live.boundary_locked"))
        if judge:
            st.caption(
                tr(
                    "live.caps",
                    calls=info["session_calls"],
                    usd=display_amount(config.budget_usd),
                    cny=display_amount(config.budget_cny),
                )
            )
        else:
            st.caption(tr("live.customer.caps", calls=info["session_calls"]))
        st.caption(tr("live.kernel_note" if judge else "live.customer.kernel_note"))
        st.caption(tr("live.no_secret" if judge else "live.customer.no_secret"))
    info = live.snapshot()
    _badge(controller, info)
    exhausted = st.session_state.pop(NOTICE_KEY, None)
    if exhausted:
        st.warning(tr("live.exhausted_notice", reason=tr("live.exhausted." + exhausted)))
    if judge:
        slot = st.empty()
        _judge_slot.set(slot)
        _render_judge(slot, live)
    return live


def _render_judge(slot, live: LiveSession) -> None:
    info = live.snapshot()
    item = live.deployment
    currency = item.currency
    with slot.container():
        st.caption(
            tr(
                "live.status",
                calls=info["calls"],
                api_calls=info["api_calls"],
                spent=display_amount(Fraction(info["spent"][currency])),
                currency=currency,
                remaining=info["calls_remaining"],
                left=display_amount(Fraction(info["remaining"][currency])),
            )
        )
        st.caption(
            tr(
                "live.reserve",
                amount=display_amount(Fraction(info["reservation"])),
                currency=currency,
                tokens=RESERVE_INPUT_TOKENS,
                output=item.max_output_tokens,
            )
        )
        if info["journal_dirs"]:
            st.caption(tr("live.journal", paths=" · ".join(info["journal_dirs"])))


def _refresh_judge() -> None:
    slot, live = _judge_slot.get(), st.session_state.get(SESSION_KEY)
    if slot is not None and isinstance(live, LiveSession):
        _render_judge(slot, live)


# --- operations: the calls one gesture made, and what the page says about them ----------


def _live_session(controller) -> LiveSession | None:
    live = st.session_state.get(SESSION_KEY)
    return live if _is_live(controller) and isinstance(live, LiveSession) else None


def operation_mark(controller) -> int | None:
    """Taken right before an operation; None while the controller plans with scripts."""
    live = _live_session(controller)
    return live.mark() if live is not None else None


def _summary(outcomes: list[dict]) -> dict[str, Any]:
    failed = [item["source"] for item in outcomes if item["failed"]]
    rejected = sum(1 for item in outcomes if item["rejected"])
    # A suggestion call that timed out answered no part of the reply: counted on its own.
    timeouts = [item for item in outcomes if item["source"] == "scripted-timeout"]
    suggesting = [item for item in timeouts if suggestion_step(item.get("step"))]
    return {
        "calls": len(outcomes),
        "recorded": sum(
            1 for item in outcomes if item["source"] == "live-recorded" and not item["rejected"]
        ),
        "timeouts": len(timeouts) - len(suggesting),
        "suggest_timeouts": len(suggesting),
        "rejected": rejected,
        "failed": failed,
        "settlement_failed": sum(1 for item in outcomes if item.get("settlement") == "failed"),
        "overrun": sum(1 for item in outcomes if item.get("overrun")),
        "unreconciled": sum(1 for item in outcomes if item.get("unreconciled")),
        "deployment": outcomes[-1]["deployment"] if outcomes else None,
    }


def _accounting_notes(summary: dict | None) -> None:
    """Judge view: a cost that could not be settled, or usage beyond the pre-send bound.

    C3: when both happened to one call the ledger may be lower than the real cost; that
    note replaces the two others, which would each be false then.
    """
    if not isinstance(summary, dict) or not view_mode.is_judge():
        return
    if summary.get("unreconciled"):
        st.warning(tr("live.note.unreconciled"))
        return
    if summary.get("settlement_failed"):
        st.warning(tr("live.note.settlement_failed"))
    if summary.get("overrun"):
        st.warning(tr("live.note.overrun"))


def _outcomes_since(mark: int | None, error: BaseException | None = None) -> list[dict]:
    """The attempts one operation made, indexed in their journals before returning.

    An output error raised after every attempt succeeded at the client means the agent
    refused the model's content (a schema or signed-handle check); that refusal is noted
    on the last attempt first, so neither the page nor the index counts it as accepted.
    """
    live = st.session_state.get(SESSION_KEY)
    if mark is None or not isinstance(live, LiveSession):
        return []
    if isinstance(error, LLMOutputError):
        live.refuse_last(mark)
    outcomes = live.outcomes(mark)
    live.flush()
    return outcomes


def _failure_source(summary: dict) -> str | None:
    """Which live outcome made an operation fail; None when no model call caused it."""
    if not summary or not summary.get("calls"):
        return None
    if summary["failed"]:
        return str(summary["failed"][-1])
    if summary["rejected"]:
        return "agent-rejected"
    return None


def _notes_key(prefix: str, identifier: str) -> str:
    return f"live_notes:{prefix}:{identifier}"


def _failure_key(prefix: str, identifier: str) -> str:
    return f"live_failure:{prefix}:{identifier}"


def remember(prefix: str, identifier: str, mark: int | None) -> None:
    """Bind what this operation's live calls were to the reply they produced."""
    outcomes = _outcomes_since(mark)
    if not outcomes:
        return
    key = _notes_key(prefix, identifier)
    previous = st.session_state.get(key)
    summary = _summary(outcomes)
    if isinstance(previous, dict):
        summary = {
            name: (previous.get(name, type(value)()) + value if name != "deployment" else value)
            for name, value in summary.items()
        }
    st.session_state[key] = summary


def remember_failure(
    prefix: str, identifier: str, mark: int | None, error: BaseException | None = None
) -> None:
    """An operation on this reply failed: keep its card from being confirmed again."""
    summary = _summary(_outcomes_since(mark, error))
    st.session_state[_failure_key(prefix, identifier)] = {
        "live": summary,
        "source": _failure_source(summary),
    }


def recorded_failure(prefix: str, identifier: str) -> dict | None:
    value = st.session_state.get(_failure_key(prefix, identifier))
    return value if isinstance(value, dict) else None


def _failure_line(source: str | None) -> bool:
    """Name the live model's part in a failed operation; nothing if it had none."""
    if source is None:
        return False
    reason = tr(
        "live.reason_of." + source if source in FAILURE_REASONS else "live.reason_of.unknown"
    )
    if view_mode.is_judge():
        st.error(tr("live.failure", source=source, reason=reason))
    else:
        st.error(tr("live.customer.failure", reason=reason))
    return True


def render_recorded_failure(failure: dict) -> None:
    """The failure block that replaces a card's confirm button after a failed gesture."""
    st.error(
        tr("The operation was not completed. Review current facts and retry from a fresh proposal.")
    )
    _failure_line(failure.get("source"))
    _accounting_notes(failure.get("live"))
    st.caption(tr("live.failure_cancel"))


def render_live_failure(controller, error: BaseException | None = None) -> bool:
    """After an operation in the page body failed: name the live model's part in it.

    Only the calls made since this run's live controls were drawn count, so an earlier
    failure is never announced again and a failure that reached no model says nothing.
    """
    summary = _summary(_outcomes_since(_run_mark.get(), error))
    _refresh_judge()
    _accounting_notes(summary)
    return _failure_line(_failure_source(summary))


def render_response_notes(controller, response: dict, prefix: str) -> None:
    """What the live model did and did not produce for this reply."""
    notes = st.session_state.get(_notes_key(prefix, response["response_id"]))
    judge = view_mode.is_judge()
    if isinstance(notes, dict) and notes.get("timeouts"):
        st.warning(tr("live.note.partly_scripted", count=notes["timeouts"]))
    if isinstance(notes, dict) and notes.get("suggest_timeouts"):
        st.caption(tr("live.note.suggest_timeout"))
    _accounting_notes(notes)
    if isinstance(notes, dict) and notes.get("calls") and judge:
        st.caption(
            tr(
                "live.note.calls",
                calls=notes["calls"],
                recorded=notes["recorded"],
                timeouts=notes["timeouts"],
                rejected=notes["rejected"],
            )
        )
    if response.get("planner_mode") in {None, "scripted"}:
        return
    if response.get("intent") == "investment":
        st.caption(tr("live.note.wealth_scripted"))
    elif judge and response.get("status") == "clarify":
        st.caption(tr("live.note.clarify"))
