"""The live-model switch behind the web front end (S7), one web session at a time.

A second implementation of the glue in ``ui/live_controls.py`` (design §2.8): every
decision comes from ``LiveSession``; this module only says when it is applied and what
the page shows. The switch is applied around business and live POSTs only, never on a
GET, a view or locale change, a reset or a login, and the planner changes only at the
transaction boundary. Each operation is attributed to the calls it made itself (read
from the live session's own mark), so a note, a failure line or a planner log entry is
always about that operation. The page never holds a key or an access code: the browser
sends a proof for a single-use challenge, and nothing here keeps the proof.

Public mode (``ws.public``, set by the session table from the ``PublicConfig``) adds its
own gate in front of every grant and every client (RW-8 items 6, 8, 10, 11): the session's
deployment must be one the public site allows (``ws.public_deployments``), the process
environment must hold a well-formed verifier pair, the live caps must be within the
public bounds and the spend ledger must answer (a public site never starts one: R8 S2);
otherwise the live POSTs answer ``live_unavailable`` and no client is ever
built. The session's ``LiveSession`` is built without the ledger's shared wrong-code lock,
since the code is drawn at random by the tool and attempts are limited per address.
"""

import os
from collections.abc import Generator
from contextlib import contextmanager
from fractions import Fraction

from corridor.schemas.money import display_amount
from corridor.web import compat
from corridor.web import public as public_mode
from corridor.web.catalog import wtr
from corridor.web.compat import (
    DEPLOYMENTS,
    FAILURE_REASONS,
    PROCESS,
    RESERVE_INPUT_TOKENS,
    SITE_DEPLOYMENTS,
    BudgetExceeded,
    JournalClient,
    LiveConfig,
    LiveSession,
    LLMClient,
    LLMConfigurationError,
    LLMOutputError,
    read_credential,
    tr,
)
from corridor.web.state import unwrap

# The exceptions an operation records as a failed gesture (the Streamlit page's set).
FAILURES = (ValueError, RuntimeError, KeyError, TimeoutError, OSError)
GENERIC_FAILURE = (
    "The operation was not completed. Review current facts and retry from a fresh proposal."
)
UNAVAILABLE = frozenset({
    "disabled",
    "missing_journal",
    "inherited_env",
    "missing_credentials",
    "weak_code",
})
LOCKED = frozenset({"locked", "locked_all"})
UNLOCKED = {
    "accepted": "live.unlocked",
    "invalid": "live.denied",
    "locked": "live.locked_out",
    "locked_all": "live.locked_out_all",
    # The challenge was used up by another attempt: the next click fetches a new one.
    "stale": "live.reason.access_required",
}
# A slot of ws.live_notes that is never a reply's: what this POST says on the panel once.
PANEL = ("", "panel")


def _errors():
    # The error table belongs to the shell line; imported on use so this module loads alone.
    from corridor.web import errors

    return errors


def _judge() -> bool:
    return compat.get_audience() == "judge"


def _config() -> tuple[LiveConfig | None, bool]:
    """The process configuration now, and whether it no longer parses."""
    try:
        return LiveConfig.from_environ(), False
    except LLMConfigurationError:
        return None, True


def _is_live(controller) -> bool:
    return isinstance(getattr(controller, "client", None), JournalClient)


def _scripted(client) -> bool:
    return (
        isinstance(client, LLMClient)
        and not isinstance(client, JournalClient)
        and client.mode == "scripted"
    )


def _usable(ws, enabled: bool) -> bool:
    """The controller's live client may still call: switched on, current and granted."""
    return (
        enabled
        and _is_live(ws.controller)
        and ws.live is not None
        and ws.live.is_current(ws.controller.client)
    )


def _boundary(ws) -> bool:
    return ws.controller.at_transaction_boundary() and not (
        ws.live is not None and ws.live.in_flight
    )


def _deployment(ws) -> str:
    return ws.controller.client.deployment.label


# --- public mode's gate ---------------------------------------------------------------------


def _public(ws) -> bool:
    return bool(getattr(ws, "public", False))


def _allowed_deployment(ws, deployment_id: str) -> bool:
    """Outside public mode every deployment of the table; in it only the site's own."""
    allowed = getattr(ws, "public_deployments", None)
    return allowed is None or deployment_id in allowed


def _new_live(ws, config: LiveConfig) -> LiveSession:
    """A session's live access. In public mode without the ledger's shared lock (RW-8 item
    8), never starting a ledger of its own (R8 S2), and selecting deepseek-flash by default
    from the site's separate model table (RW-12);
    the gate below still checks it before a client."""
    public = _public(ws)
    live = LiveSession(
        config,
        environ=PROCESS,
        global_lock=not public,
        ledger_create=not public,
        deployments=SITE_DEPLOYMENTS if public else DEPLOYMENTS,
    )
    if public:
        live.select("deepseek-flash")
    return live


def _listed(ws, config: LiveConfig, deployment_id: str) -> bool:
    if not _allowed_deployment(ws, deployment_id):
        return False
    if not _public(ws):
        return True
    item = next((item for item in SITE_DEPLOYMENTS if item.id == deployment_id), None)
    path = config.credential_path(item) if item is not None else None
    return path is not None and read_credential(path) is not None


def public_disclosure(ws) -> str:
    item = (
        ws.live.deployment
        if ws.live is not None
        else next(item for item in DEPLOYMENTS if item.id == "deepseek-flash")
    )
    return wtr("web.vm.public.disclosure", deployment=item.label, provider=item.provider)


def _public_closed(ws, live: LiveSession, config: LiveConfig) -> str | None:
    """Why public mode refuses live access right now, or None when it may proceed:
    ``deployment`` (the session's deployment is not the site's), ``access`` (the process
    environment holds no well-formed verifier pair: missing, half, malformed or a plain
    code), ``caps`` (the live caps exceed the public bounds), ``ledger`` (the spend ledger
    is lost or does not answer: live stays closed until an operator has reconciled it, the
    site keeps serving the scripted planner). Checked before a challenge, a grant, a switch
    and a client; outside public mode always None."""
    if not _public(ws):
        return None
    if not _allowed_deployment(ws, live.deployment.id):
        return "deployment"
    if not public_mode.access_configured(os.environ):
        return "access"
    if public_mode.public_caps(config) is not None:
        return "caps"
    if not live.ledger_ready():
        return "ledger"
    return None


def _caps_text(ws, config: LiveConfig, info: dict, judge: bool) -> str:
    if not judge:
        return tr("live.customer.caps", calls=info["session_calls"])
    if _public(ws):
        # The product's line says a new journal directory starts the total again; a public
        # site has only the one directory (R8 S2).
        return wtr(
            "web.vm.live.public_caps",
            calls=info["session_calls"],
            cny=display_amount(config.budget_cny),
        )
    return tr(
        "live.caps",
        calls=info["session_calls"],
        usd=display_amount(config.budget_usd),
        cny=display_amount(config.budget_cny),
    )


def _closed_text(closed: str, judge: bool) -> str:
    if not judge:
        return tr("live.customer.reason.unavailable")
    return wtr("web.vm.live.public_closed." + closed)


# --- one-time lines ----------------------------------------------------------------------


def _notice(ws, level: str, text: str) -> None:
    ws.pending_notices.append({
        "id": f"{ws.generation}:{ws.revision}:{len(ws.pending_notices)}",
        "level": level,
        "text": text,
    })


def _slot(ws) -> dict:
    stamp = (ws.generation, ws.revision)
    slot = ws.live_notes.get(PANEL)
    if not isinstance(slot, dict) or slot.get("stamp") != stamp:
        slot = ws.live_notes[PANEL] = {"stamp": stamp, "result": None, "notice": None}
    return slot


def _shown(ws) -> dict:
    slot = ws.live_notes.get(PANEL)
    stamp = (ws.generation, ws.revision)
    return slot if isinstance(slot, dict) and slot.get("stamp") == stamp else {}


def _panel_notice(ws, level: str, text: str) -> None:
    _notice(ws, level, text)
    _slot(ws)["notice"] = text


# --- session lifecycle -------------------------------------------------------------------


def on_new(ws) -> None:
    ws.scripted_client = ws.controller.client


def on_reset(old, new) -> None:
    """The same live session moves to the new tab session (counts, wrong codes and
    journals carry over); the switch is off and the old client is let go."""
    live, old.live = old.live, None
    old.live_toggle = new.live_toggle = False
    if getattr(new, "scripted_client", None) is None:
        on_new(new)
    new.live = live
    if live is not None:
        live.release()


def close(ws) -> None:
    ws.live_toggle = False
    if ws.live is not None:
        ws.live.release()


def _session(ws, config: LiveConfig) -> LiveSession:
    if ws.live is None:
        ws.live = _new_live(ws, config)
    else:
        ws.live.refresh(config)
    return ws.live


def _require(ws) -> LiveSession:
    config, _ = _config()
    if config is None or not config.enabled:
        raise _errors().error("live_unavailable")
    live = _session(ws, config)
    if _public_closed(ws, live, config) is not None:
        # Public mode: no challenge, grant, selection or switch while the gate is closed.
        raise _errors().error("live_unavailable")
    return live


# --- applying the switch -----------------------------------------------------------------


def _fail_closed(ws) -> None:
    """The saved scripted client is missing (impossible by construction): no new client
    is made in its place (INV-10.6); the switch goes off and the request fails."""
    ws.live_toggle = False
    if ws.live is not None:
        ws.live.release()
    raise _errors().WebInvariantError()


def _use_scripted(ws) -> None:
    """Hand the controller and its chat session back to the very scripted client the web
    session was built with (only at the transaction boundary)."""
    scripted = ws.scripted_client
    if not _scripted(scripted):
        _fail_closed(ws)
    ws.controller.set_client(scripted)
    # The chat session holds the controller itself; ``ws.controller`` is its proxy.
    if ws.session.controller is unwrap(ws.controller):
        ws.session.client = scripted
    if ws.live is not None:
        ws.live.release()


def _apply_off(ws) -> None:
    """Switched off or no longer parsing: new external calls stop at once, and the
    scripted planner takes over at the transaction boundary."""
    if ws.live is None:
        return
    ws.live.refresh(LiveConfig())
    if _is_live(ws.controller) and _boundary(ws):
        _use_scripted(ws)


def _apply_on(ws, config: LiveConfig) -> None:
    live = _session(ws, config)
    controller = ws.controller
    boundary = _boundary(ws)
    info = live.snapshot()
    # Public mode's gate, checked again right here, before any client is built (RW-8 item
    # 6: the deployment; item 11: the verifier pair; item 10: the caps).
    closed = _public_closed(ws, live, config)
    available = info["available"] and closed is None
    if ws.live_toggle and not available and boundary:
        # Live access was lost while the switch was on (a cap, above all): off it goes,
        # and a cap says so once.
        ws.live_toggle = False
        if closed is not None:
            _panel_notice(ws, "error", _closed_text(closed, _judge()))
        elif info["reason"] == "budget_exhausted":
            cause = "calls" if info["calls_remaining"] == 0 and info["calls"] > 0 else "spend"
            text = tr("live.exhausted_notice", reason=tr("live.exhausted." + cause))
            _panel_notice(ws, "warning", text)
    wanted = ws.live_toggle and available
    if wanted and boundary:
        if _shown(ws).get("unbuilt"):
            # One attempt per request, as one page run makes: the next request tries again.
            return
        if not _scripted(ws.scripted_client):
            _fail_closed(ws)
        if not _allowed_deployment(ws, live.deployment.id):
            # Unreachable past the gate above; kept as the last check before a client.
            _fail_closed(ws)
        try:
            client = live.client(controller, context_guard=controller.gateway.vault.ensure_safe)
        except (BudgetExceeded, LLMConfigurationError, OSError):
            returned = _is_live(controller)
            if returned:
                _use_scripted(ws)
            key = "live.client_failed_returned" if returned else "live.client_failed"
            _panel_notice(ws, "error", tr(key))
            _slot(ws)["unbuilt"] = True
            return
        if client is not controller.client:
            controller.set_client(client)
            if ws.session.controller is unwrap(controller):
                ws.session.client = client
    elif _is_live(controller) and boundary and not wanted:
        _use_scripted(ws)


def _backfill(ws) -> None:
    # A reply's key is registered after the operation that produced it returns.
    for entry in ws.op_log:
        if entry.get("rkey") is None and entry.get("rid") is not None:
            entry["rkey"] = ws.rkey_of.get(entry["rid"])


def _state(ws, config: LiveConfig | None) -> str:
    if config is None or not config.enabled:
        return "OFF"
    if not _is_live(ws.controller):
        return "SCRIPTED"
    return "LIVE" if _usable(ws, True) else "LIVE_STALE"


def sync(ws, *, apply: bool) -> dict:
    """Apply the switch (business and live POSTs only), then report the state."""
    _backfill(ws)
    config, invalid = _config()
    enabled = config is not None and config.enabled
    if apply:
        if enabled:
            _apply_on(ws, config)
        else:
            _apply_off(ws)
    return {
        "state": _state(ws, config),
        "enabled": enabled,
        "invalid": invalid,
        "boundary": _boundary(ws),
    }


def may_call(ws) -> bool:
    """The next business call may reach a model (it then runs on the live pool)."""
    return _is_live(ws.controller) or bool(ws.live_toggle)


# --- live POSTs ----------------------------------------------------------------------------


def challenge(ws) -> str:
    return _require(ws).challenge()


def unlock(ws, nonce: str, proof: str) -> str:
    live = _require(ws)
    if not _boundary(ws):
        raise _errors().error("not_at_boundary")
    reason = live.snapshot()["reason"]
    result = reason if reason in LOCKED else live.authorize_proof(nonce, proof)
    _slot(ws)["result"] = result
    return result


def select(ws, deployment: str) -> None:
    live = _require(ws)
    if not _boundary(ws):
        raise _errors().error("not_at_boundary")
    if _is_live(ws.controller):
        raise _errors().error("live_on")
    table = SITE_DEPLOYMENTS if _public(ws) else DEPLOYMENTS
    if deployment not in {item.id for item in table} or not _listed(ws, live.config, deployment):
        # Public mode: only the site's deployment (RW-8 item 6); the handler refused it
        # before the action too, so this is the last check before the product call.
        raise _errors().error("invalid_input")
    live.select(deployment)


def toggle(ws, on: bool) -> None:
    live = _require(ws)
    if not _boundary(ws):
        raise _errors().error("not_at_boundary")
    if on and not live.snapshot()["available"]:
        raise _errors().error("live_unavailable")
    ws.live_toggle = bool(on)


# --- operations: the calls one gesture made ------------------------------------------------


class Op:
    """One gesture's planner calls; ``bind`` names the reply they produced.

    The gesture also knows its session and, after an expected failure, the one failure it
    raised: that failure's source mark points back here (RW-6 item 1).
    """

    def __init__(self, rid: str | None, ws=None) -> None:
        self.rid = rid
        self.ws = ws
        self.failure = None

    def bind(self, rid: str) -> None:
        self.rid = rid


def expected_failure(failure: BaseException, ws) -> bool:
    """Whether ``failure`` is the expected failure ``operation`` raised for a gesture of
    this session: a ``WebOperationFailed`` whose source mark is an ``Op`` holding this
    very failure and this very session (RW-6 item 1). Any other ``operation_failed`` is
    unmarked, and the handler judges it by the count of product calls instead."""
    origin = getattr(failure, "origin", None)
    return (
        isinstance(failure, _errors().WebOperationFailed)
        and type(origin) is Op
        and origin.failure is failure
        and origin.ws is ws
    )


def _outcomes(live: LiveSession, mark: int, error: BaseException | None = None) -> list[dict]:
    """The attempts one operation made, indexed in their journals before returning; an
    output error after every attempt succeeded at the client is the agent's refusal."""
    if isinstance(error, LLMOutputError):
        live.refuse_last(mark)
    outcomes = live.outcomes(mark)
    live.flush()
    return outcomes


def _failure_text(source: str | None) -> str | None:
    if source is None:
        return None
    reason = tr(
        "live.reason_of." + source if source in FAILURE_REASONS else "live.reason_of.unknown"
    )
    if _judge():
        return tr("live.failure", source=source, reason=reason)
    return tr("live.customer.failure", reason=reason)


def _accounting(summary: dict | None) -> list[str]:
    """Judge view: a cost that could not be settled, or usage beyond the pre-send bound."""
    if not isinstance(summary, dict) or not _judge():
        return []
    lines = []
    if summary.get("settlement_failed"):
        lines.append(tr("live.note.settlement_failed"))
    if summary.get("overrun"):
        lines.append(tr("live.note.overrun"))
    return lines


def _remember(ws, key: tuple[str, str], summary: dict) -> None:
    previous = ws.live_notes.get(key)
    if isinstance(previous, dict):
        summary = {
            name: (previous.get(name, type(value)()) + value if name != "deployment" else value)
            for name, value in summary.items()
        }
    ws.live_notes[key] = summary


@contextmanager
def _closing() -> Generator[None, None, None]:
    """The bookkeeping that closes a gesture after its call ran. A refusal or a contained
    result raised from it would say that the call changed nothing, so it is an unknown
    result instead (RW-5 item 1); an unknown or broken result passes as it is, and any
    other exception goes to the handler, which treats it as unknown."""
    errors = _errors()
    try:
        yield
    except errors.WebError as error:
        if error.status >= 500 and not error.contained:
            raise
        raise errors.WebOutcomeUnknown() from None


def _record_failure(ws, prefix: str, op: Op, live, mark, error: BaseException) -> None:
    """The expected failure of a gesture: on its reply, or said once on the page."""
    summary = None
    if live is not None and mark is not None:
        summary = compat._summary(_outcomes(live, mark, error))
    source = compat._failure_source(summary) if summary else None
    if op.rid is not None:
        ws.failures[(prefix, op.rid)] = {"live": summary, "source": source}
    else:
        # No reply exists: the page says so once, never on an older reply.
        _notice(ws, "error", tr(GENERIC_FAILURE))
        for text in _accounting(summary):
            _notice(ws, "warning", text)
        line = _failure_text(source)
        if line is not None:
            _notice(ws, "error", line)


def _record_success(ws, prefix: str, op: Op, live, mark, bind: bool) -> None:
    """The calls a finished gesture made, bound to its reply."""
    outcomes = _outcomes(live, mark) if live is not None and mark is not None else []
    summary = compat._summary(outcomes) if outcomes else None
    if bind and op.rid is not None:
        if summary is not None:
            _remember(ws, (prefix, op.rid), summary)
        ws.op_log.append({
            "rkey": ws.rkey_of.get(op.rid),
            "rid": op.rid,
            "planner": summary["deployment"] if summary and summary["calls"] else "scripted",
            "summary": summary,
        })


@contextmanager
def operation(
    ws, prefix: str, rid: str | None = None, *, bind: bool = True
) -> Generator[Op, None, None]:
    """One gesture: its live calls are bound to its reply, or its failure is recorded.

    Only ``FAILURES`` are recorded as the gesture's expected failure (operation_failed). A
    ``WebError`` raised from inside the gesture, after its call began, is never answered
    as a refusal that changed nothing: unless it is already an unknown result, it becomes
    one (RW-4''''). The same holds for the bookkeeping that closes the gesture, after a
    success and after an expected failure (RW-5 item 1). Other exceptions pass unchanged;
    the handler treats them as unknown. A refusal at the entry, before the gesture runs,
    keeps its status. The expected failure carries the gesture as its source mark, and
    the gesture holds that failure (RW-6 item 1).
    """
    op = Op(rid, ws)
    live = ws.live if _is_live(ws.controller) else None
    mark = live.mark() if live is not None else None
    try:
        yield op
    except _errors().WebError as error:
        if error.status >= 500 and not error.contained:
            raise
        raise _errors().WebOutcomeUnknown() from None
    except FAILURES as error:
        with _closing():
            _record_failure(ws, prefix, op, live, mark, error)
        failure = op.failure = _errors().WebOperationFailed(origin=op)
        raise failure from error
    with _closing():
        _record_success(ws, prefix, op, live, mark, bind)


def notes(ws, response: dict, prefix: str) -> list[dict]:
    """What the live model did and did not produce for this reply."""
    summary = ws.live_notes.get((prefix, response["response_id"]))
    judge = _judge()
    lines = []
    if isinstance(summary, dict) and summary.get("timeouts"):
        lines.append(("warning", tr("live.note.partly_scripted", count=summary["timeouts"])))
    if isinstance(summary, dict) and summary.get("suggest_timeouts"):
        lines.append(("info", tr("live.note.suggest_timeout")))
    lines.extend(("warning", text) for text in _accounting(summary))
    if isinstance(summary, dict) and summary.get("calls") and judge:
        text = tr(
            "live.note.calls",
            calls=summary["calls"],
            recorded=summary["recorded"],
            timeouts=summary["timeouts"],
            rejected=summary["rejected"],
        )
        lines.append(("info", text))
    if response.get("planner_mode") not in {None, "scripted"}:
        if response.get("intent") == "investment":
            lines.append(("info", tr("live.note.wealth_scripted")))
        elif judge and response.get("status") == "clarify":
            lines.append(("info", tr("live.note.clarify")))
    key = ws.rkey_of.get(response["response_id"], response["response_id"])
    return [
        {"id": f"{ws.generation}:{ws.revision}:{key}.live.{index}", "level": level, "text": text}
        for index, (level, text) in enumerate(lines)
    ]


def failure_vm(ws, prefix: str, rid: str) -> dict | None:
    """The failure block that replaces a card's confirm after a failed gesture."""
    failure = ws.failures.get((prefix, rid))
    if not isinstance(failure, dict):
        return None
    lines = [tr(GENERIC_FAILURE)]
    line = _failure_text(failure.get("source"))
    if line is not None:
        lines.append(line)
    lines.extend(_accounting(failure.get("live")))
    return {"kind": "failed", "lines": lines, "cancel_hint": tr("live.failure_cancel")}


def page_failure(ws) -> list[dict]:
    notices = list(ws.pending_notices)
    ws.pending_notices.clear()
    return notices


# --- what the page shows ---------------------------------------------------------------------


def _badge(ws, enabled: bool, info: dict | None) -> dict:
    if not _is_live(ws.controller):
        return {"kind": "demo", "text": tr("live.badge.scripted")}
    exhausted = info is not None and info["reason"] == "budget_exhausted"
    key = (
        "live.badge.live_revoked"
        if not _usable(ws, enabled)
        else "live.badge.live_exhausted"
        if exhausted
        else "live.badge.live"
    )
    return {"kind": "model", "text": tr(key, deployment=_deployment(ws))}


def badge(ws) -> dict:
    """The planner badge describes the banking chat planner (INV-10.8)."""
    config, _ = _config()
    enabled = config is not None and config.enabled
    attached = enabled and ws.live is not None and _is_live(ws.controller)
    return _badge(ws, enabled, ws.live.snapshot() if attached else None)


def corner(ws) -> str:
    text = tr("simulation_notice")
    config, _ = _config()
    if _usable(ws, config is not None and config.enabled):
        text += " · " + tr("live.corner", deployment=_deployment(ws))
    return text


def judge_strip(ws) -> str | None:
    if not _judge():
        return None
    if not _is_live(ws.controller):
        return tr("polish.view.judge_strip")
    config, _ = _config()
    if _usable(ws, config is not None and config.enabled):
        return tr("live.judge_strip", deployment=_deployment(ws))
    return tr("live.judge_strip_revoked", deployment=_deployment(ws))


def scope_note(ws, scope: str) -> str | None:
    """On the wealth page or a demo preset while the banking planner is live: their badge
    describes their own scripted planner, and this line says where the live one is."""
    if scope not in {"wealth", "preset"} or not _is_live(ws.controller):
        return None
    return tr("live.scope." + scope, deployment=_deployment(ws))


def _reason_line(ws, live: LiveSession, info: dict, boundary: bool, judge: bool) -> str:
    reason = info["reason"]
    controller = ws.controller
    if _is_live(controller) and not live.is_current(controller.client):
        return tr("live.reason.revoked_pending")
    if reason in UNAVAILABLE:
        if not judge:
            return tr("live.customer.reason.unavailable")
        if reason == "inherited_env":
            # A count, not the names: the judge view may be recorded (R1-E15).
            return wtr("web.vm.live.inherited_env", count=len(info["inherited_env"]))
        return tr("live.reason." + reason)
    if reason == "ready" and _is_live(controller):
        if _public(ws):
            # What leaves a public site, as measured (RW-9 item 2); the product's line says
            # less than leaves.
            return wtr("web.vm.live.public_on", deployment=_deployment(ws))
        return tr("live.reason.on", deployment=_deployment(ws))
    if reason == "budget_exhausted" and _is_live(controller) and not boundary:
        return tr("live.reason.budget_pending")
    return tr("live.reason." + reason)


def _counters(live: LiveSession, info: dict) -> list[str]:
    item = live.deployment
    currency = item.currency
    lines = [
        tr(
            "live.status",
            calls=info["calls"],
            api_calls=info["api_calls"],
            spent=display_amount(Fraction(info["spent"][currency])),
            currency=currency,
            remaining=info["calls_remaining"],
            left=display_amount(Fraction(info["remaining"][currency])),
        ),
        tr(
            "live.reserve",
            amount=display_amount(Fraction(info["reservation"])),
            currency=currency,
            tokens=RESERVE_INPUT_TOKENS,
            output=item.max_output_tokens,
        ),
    ]
    if info["journal_dirs"]:
        lines.append(tr("live.journal", paths=" · ".join(info["journal_dirs"])))
    return lines


def counters(ws) -> list[str]:
    """The judge view's live counters (none while the switch is off or not yet used)."""
    config, _ = _config()
    if not _judge() or config is None or not config.enabled or ws.live is None:
        return []
    return _counters(ws.live, ws.live.snapshot())


def _unlock_text(result: str | None, judge: bool) -> str | None:
    if result is None:
        return None
    if result in UNLOCKED:
        return tr(UNLOCKED[result])
    return tr("live.unlock_unavailable" if judge else "live.customer.reason.unavailable")


def view(ws) -> dict:
    """The live panel (contract §5.3). It only reads, since a GET calls it too."""
    config, invalid = _config()
    judge = _judge()
    attached = _is_live(ws.controller)
    boundary = _boundary(ws)
    shown = _shown(ws)
    panel = {
        "enabled": False,
        "config_invalid": invalid,
        "title": tr("live.expander_on", deployment=_deployment(ws))
        if attached
        else tr("live.expander"),
        "intro": tr("live.intro" if judge else "live.customer.intro"),
        "off_text": None,
        "deployments": [],
        "selected": None,
        "unlock": {
            "disabled": True,
            "label": tr("live.access_code"),
            "button": tr("live.unlock"),
            "result_text": None,
        },
        "revoked_text": None,
        "toggle": {"on": bool(ws.live_toggle), "disabled": True, "label": tr("live.toggle")},
        "reason_text": None,
        "boundary": boundary,
        "boundary_text": None,
        "caps_text": None,
        "kernel_note": None,
        "no_secret": None,
        "badge": None,
        "scope_note": None,
        "notice": shown.get("notice"),
    }
    if config is None or not config.enabled:
        # As the Streamlit page with the switch off: the badge and at most two lines.
        lines = []
        if invalid:
            lines.append(tr("live.config_invalid_live" if attached else "live.config_invalid"))
        if attached:
            lines.append(tr("live.reason.revoked_pending"))
        elif judge:
            lines.append(tr("live.off_hint"))
        panel["off_text"] = lines[0] if lines else None
        panel["reason_text"] = lines[1] if len(lines) > 1 else None
        panel["badge"] = _badge(ws, False, None)
        return panel
    # Before a business or live POST builds this session's access, a fresh one is shown.
    live = ws.live if ws.live is not None else _new_live(ws, config)
    info = live.snapshot()
    # Public mode: the panel lists the site's deployment only, and says why the gate is
    # closed instead of the product's reason (RW-8 items 6, 10, 11).
    closed = _public_closed(ws, live, config)
    current = next(item for item in info["deployments"] if item["id"] == info["deployment"])
    panel.update(
        enabled=True,
        deployments=[
            {
                "id": item["id"],
                "label": item["label"],
                **(
                    {"credential_text": tr("live.credential." + item["credential"])}
                    if judge
                    else {}
                ),
            }
            for item in info["deployments"]
            if _listed(ws, config, item["id"])
        ],
        selected=info["deployment"],
        revoked_text=tr("live.revoked.credentials")
        if info["revoked"] == "credentials" and not info["authorized"]
        else None,
        reason_text=_reason_line(ws, live, info, boundary, judge)
        if closed is None
        else _closed_text(closed, judge),
        boundary_text=None if boundary else tr("live.boundary_locked"),
        caps_text=_caps_text(ws, config, info, judge),
        kernel_note=tr("live.kernel_note" if judge else "live.customer.kernel_note"),
        no_secret=tr("live.no_secret" if judge else "live.customer.no_secret"),
        badge=_badge(ws, True, info),
    )
    panel["unlock"].update(
        disabled=not boundary or info["reason"] in LOCKED or closed is not None,
        result_text=_unlock_text(shown.get("result"), judge),
    )
    panel["toggle"]["disabled"] = not info["available"] or not boundary or closed is not None
    if _public(ws):
        panel["kernel_note"] += " " + tr("live.site_selection")
    if judge:
        panel["price"] = tr(
            "live.price",
            model=current["model"],
            price_in=current["price_in"],
            price_out=current["price_out"],
            currency=current["currency"],
            note=tr(current["price_note"]),
        )
        panel["counters"] = _counters(live, info)
    return panel
