"""An isolated scripted authorization experiment, driven only by explicit requests.

The lab prepares real synthetic-controller cards and uses the existing device service
for every approval and MFA step. It opens only in the judge view and only while the
``lab`` capability is open (R2): the customer view never receives a lab body, and a
closed capability revokes a running experiment. Its GETs return snapshots made by the
preceding POST; they never construct a controller, refresh a task or issue a gesture.

The child's simulated SMS is shown only by the phone page of the child's own pairing
(``/phone/<pair>``, a separate document on the pairing allow list while the experiment
lives), never by a session API of the main page. A failure inside the experiment closes
the child and drops its cached views; the parent banking session is not distrusted.
"""

from copy import deepcopy
from typing import Any

from corridor.web import catalog, compat, contracts, refresh, service, state, tasks
from corridor.web.errors import WebChildOutcomeUnknown, WebError, error
from corridor.web.vm.app import app_state
from corridor.web.vm.evidence import receipts_vm

SLOT = "authorization_lab"
SOURCE = "synthetic_controller_gateway"
FEATURE = "lab"
VIEW = "judge"
CONTEXTS = tuple((VIEW, locale) for locale in ("zh-CN", "en"))
# Experiments a parent session may start: each start builds new synthetic controllers.
STARTS = 24
# A controller status outside the contract's set is shown as unknown (as on main routes).
KNOWN = frozenset(contracts.ResultStatus.__args__)
REFUSALS = frozenset({
    "not_found",
    "not_current",
    "gesture_unknown",
    "gesture_mismatch",
    "card_changed",
    "gesture_closed",
    "facts_incomplete",
    "gate_stale",
    "not_applicable",
    "invalid_input",
})


def open_to(ws) -> None:
    """Refuse every lab request outside the judge view (R2-S1): no lab body is built or
    returned there, so no internal word or transaction id reaches the customer view."""
    if ws.view != VIEW:
        raise error("not_applicable")


def view(ws) -> dict[str, Any]:
    held = ws.demos.get(SLOT)
    shown = held["views"].get((ws.view, ws.locale)) if held is not None else None
    if shown is None:
        return {"active": False, "mode": "scripted"}
    shown = deepcopy(shown)
    if ws.untrusted or held["session"].untrusted:
        # Even if an error prevented a final projection, no cached token is offered.
        shown["can_change"] = False
        shown["last_attempt"] = deepcopy(held["last_attempt"])
        for task in shown["state"]["device"]["tasks"]:
            task["gesture"] = None
    return shown


def _entry(ws, generation: str) -> dict[str, Any]:
    open_to(ws)
    held = ws.demos.get(SLOT)
    if held is None:
        raise error("not_applicable")
    if not tasks._same(held["session"].generation, generation):
        raise error("stale_generation")
    if held["session"].untrusted:
        raise error("not_applicable")
    return held


def _task(shown: dict[str, Any]) -> dict[str, Any] | None:
    return next((task for task in shown["device"]["tasks"] if task["current"]), None)


def _prompt(scenario: str, *, changed: bool = False) -> str:
    # Like source_utterance, use a stable domestic-parser input across display locales.
    key = f"web.ui.lab.prompt.{'changed.' if changed else ''}{scenario}"
    try:
        return catalog.WEB_MESSAGES[key]["zh-CN"]
    except KeyError:
        raise error("not_applicable") from None


def _audit(snapshot: dict[str, Any]) -> bool | None:
    # A missing check is "not provided", never "failed" (R2-E8).
    value = snapshot.get("audit_valid")
    return value if isinstance(value, bool) else None


def _snapshot(ws, held: dict[str, Any], operation: str, status: str) -> dict[str, Any]:
    child = held["session"]
    child.view, child.locale = ws.view, ws.locale
    child.revision += 1
    with catalog.bound(child):
        refresh.refresh(child)
        snapshot = child.controller.snapshot()
    current = next(
        (task for task in tasks.collect(child) if task.rid == child.current.get("domestic")), None
    )
    facts = (
        [
            compat.business_facts(card)
            for card in compat.authoritative_cards(child.controller, current.identity)
        ]
        if current
        else []
    )
    if held.get("before_facts") is not None and facts:
        held["card_changed"] = held["before_facts"] != facts
    held["timeline"].append({
        "operation": operation,
        "status": status,
        "dispatches": int(snapshot.get("dispatch_count", 0)),
        "bank_receipts": sum(r.get("receipt_type") == "bank" for r in snapshot.get("receipts", [])),
        "audit_valid": _audit(snapshot),
    })
    held["timeline"] = held["timeline"][-16:]
    views = {}
    for context in CONTEXTS:
        child.view, child.locale = context
        with catalog.bound(child):
            shown = app_state(child)
            task = _task(shown)
            cards = task["cards"] if task else []
            if cards:
                held["cards"][context] = deepcopy(cards[0])
            views[context] = {
                "active": True,
                "mode": "scripted",
                "source": SOURCE,
                "scenario": held["scenario"],
                "session_generation": child.generation,
                "revision": child.revision,
                "state": shown,
                "card": deepcopy(held["cards"].get(context)),
                "previous": deepcopy(held["previous"].get(context)),
                "previous_attempted": held["previous_attempted"],
                "can_change": not held["previous"]
                and held["last_attempt"] is None
                and bool(task and task.get("gesture")),
                "last_attempt": deepcopy(held["last_attempt"]),
                "timeline": deepcopy(held["timeline"]),
                "proof": {
                    "dispatches": int(snapshot.get("dispatch_count", 0)),
                    "bank_receipts": receipts_vm(snapshot),
                    "audit_valid": _audit(snapshot),
                    "card_changed": held["card_changed"],
                    "source": SOURCE,
                },
            }
    child.view, child.locale = ws.view, ws.locale
    held["views"] = views
    return view(ws)


def retire(ws, *, quiet: bool = False) -> None:
    """Revoke the child before any parent reset, close, eviction, Stop or restart.

    ``quiet`` skips the child controller's own Stop (a read revoking a closed capability
    calls no controller); the child is unreachable either way once its slot is gone.
    """
    held = getattr(ws, "demos", {}).pop(SLOT, None)
    if held is None:
        return
    child = held["session"]
    # Removing the sole reference first prevents a partially failed cleanup being reused.
    child.untrusted = True
    child.gestures.clear()
    child.gates.clear()
    child.gate_eval.clear()
    try:
        pairs = held.get("pairs")
        if pairs is not None:
            pairs.revoke(child.pair)
        if not quiet:
            with catalog.bound(child):
                service.stop_all(child)
    finally:
        state.retire_pair(child)


def _close(ws, held: dict[str, Any], operation: str, attempt: dict[str, Any]) -> None:
    """An uncertain result inside the child: no cached token is offered again. The final
    projection is best effort; when it fails too, the whole experiment is dropped."""
    child = held["session"]
    child.untrusted = True
    child.gestures.clear()
    held["last_attempt"] = attempt
    try:
        _snapshot(ws, held, operation, attempt["status"])
    except Exception:
        retire(ws, quiet=True)


def _contain(exc: Exception) -> WebError:
    """What the parent answers for a failure inside the child: a known refusal or failure
    below 500 as it is, anything else as the child's own unknown outcome (never a parent
    distrust, R2-E7)."""
    if isinstance(exc, WebError) and exc.status < 500:
        return exc
    return WebChildOutcomeUnknown()


def start(ws, scenario: str, pairs: Any = None) -> dict[str, Any]:
    open_to(ws)
    if not compat.logged_in(ws.controller):
        # The experiment opens from a logged-in banking session only (R2-E7).
        raise error("not_applicable")
    text = _prompt(scenario)
    started = int(ws.business.get("lab_starts", 0))
    if started >= STARTS:
        raise error("not_applicable")
    ws.business["lab_starts"] = started + 1
    retire(ws)
    child = state.new_web_session(ws.view, ws.locale, browser_id=ws.browser_id, tab_id=ws.tab_id)
    child.features = frozenset({"shortcut:transfer"})
    held = {
        "session": child,
        "pairs": pairs,
        "scenario": scenario,
        "cards": {},
        "previous": {},
        "old_gesture": None,
        "previous_attempted": False,
        "last_attempt": None,
        "timeline": [],
        "views": {},
        "card_changed": False,
        "before_facts": None,
    }
    ws.demos[SLOT] = held
    try:
        if pairs is not None:
            # The child's phone page shows its own inbox only while the experiment lives.
            pairs.allow(child.pair)
        with catalog.bound(child):
            service.login(child)
            service.send_message(child, text, 0, "lab-initial-0001")
        return _snapshot(ws, held, "start", "prepared")
    except Exception as exc:
        retire(ws)
        raise _contain(exc) from None


def change(ws, generation: str) -> dict[str, Any]:
    held = _entry(ws, generation)
    if not view(ws)["can_change"]:
        raise error("not_applicable")
    text = _prompt(held["scenario"], changed=True)
    task = _task(held["views"][(ws.view, ws.locale)]["state"])
    if task is None or not task.get("gesture") or task["gesture"]["op"] != "confirm":
        raise error("not_applicable")
    child = held["session"]
    actual = tasks.find(child, task["key"])
    before = [
        compat.business_facts(card)
        for card in compat.authoritative_cards(child.controller, actual.identity)
    ]
    try:
        held["old_gesture"] = {"tkey": task["key"], **task["gesture"]}
        held["before_facts"] = before
        for context, shown in held["views"].items():
            old_task = deepcopy(_task(shown["state"]))
            old_task["gesture"] = None
            old_task["current"] = False
            held["previous"][context] = {"card": deepcopy(shown["card"]), "task": old_task}
        with catalog.bound(child):
            service.chat_stop(child)
            service.send_message(child, text, 0, "lab-changed-0001")
        return _snapshot(ws, held, "change", "prepared")
    except Exception as exc:
        # The withdrawal began: the child and every cached view are dropped (R2-E7).
        retire(ws)
        raise _contain(exc) from None


def _known(status: Any) -> str:
    return status if isinstance(status, str) and status in KNOWN else "unknown"


def _attempt(ws, held, *, operation, tkey, op, gesture, card_version, code=None):
    child = held["session"]
    try:
        with catalog.bound(child):
            if op == "confirm":
                result = service.device_confirm(child, tkey, gesture, card_version)
            elif op == "mfa_request":
                result = service.device_mfa_request(child, tkey, gesture, card_version)
            elif op == "mfa_verify":
                result = service.device_mfa_verify(child, tkey, gesture, card_version, code)
            else:
                raise error("invalid_input")
    except WebError as exc:
        if exc.code in REFUSALS:
            held["last_attempt"] = {
                "operation": operation,
                "status": "refused",
                "code": exc.code,
                "operation_status": exc.status,
            }
            return _safe_snapshot(ws, held, operation, "refused")
        _close(
            ws,
            held,
            operation,
            {
                "operation": operation,
                "status": "unknown" if exc.status >= 500 else "failed",
                "code": exc.code,
                "operation_status": exc.status,
            },
        )
        raise _contain(exc) from None
    except Exception:
        _close(
            ws,
            held,
            operation,
            {
                "operation": operation,
                "status": "unknown",
                "code": "outcome_unknown",
                "operation_status": 500,
            },
        )
        raise WebChildOutcomeUnknown() from None
    status = _known(result.get("status") if isinstance(result, dict) else None)
    held["last_attempt"] = {
        "operation": operation,
        "status": status,
        "code": None,
        "operation_status": 200,
    }
    return _safe_snapshot(ws, held, operation, status)


def _safe_snapshot(ws, held, operation: str, status: str) -> dict[str, Any]:
    try:
        return _snapshot(ws, held, operation, status)
    except Exception:
        retire(ws, quiet=True)
        raise WebChildOutcomeUnknown() from None


def gesture(ws, generation: str, **values) -> dict[str, Any]:
    return _attempt(ws, _entry(ws, generation), operation=values["op"], **values)


def _verify_only(child, target: str, tkey: str, old: dict[str, Any]) -> dict[str, Any]:
    """The saved old request checked as the device service would check it, and nothing
    more: ``tasks.verify`` consumes nothing and never calls a controller (R2-E5)."""
    try:
        with catalog.bound(child):
            tasks.verify(child, tkey, old["op"], old["token"], old["card_version"])
    except WebError as exc:
        if exc.code not in REFUSALS:
            raise
        return {
            "target": target,
            "accepted": False,
            "code": exc.code,
            "operation_status": exc.status,
        }
    return {"target": target, "accepted": True, "code": None, "operation_status": 200}


def try_previous(ws, generation: str) -> dict[str, Any]:
    """Two checks of the saved old confirmation: on its own (withdrawn) task, and on the
    current card's task. Neither reaches the controller; if either were accepted, the
    experiment has failed and the child is closed."""
    held = _entry(ws, generation)
    old = held["old_gesture"]
    if old is None or held["previous_attempted"]:
        raise error("not_applicable")
    held["previous_attempted"] = True
    child = held["session"]
    current = _task(held["views"][(ws.view, ws.locale)]["state"])
    targets = [("withdrawn_request", old["tkey"])]
    if current is not None and current["key"] != old["tkey"]:
        targets.append(("current_card", current["key"]))
    try:
        checks = [_verify_only(child, target, tkey, old) for target, tkey in targets]
    except Exception:
        _close(
            ws,
            held,
            "try_previous",
            {
                "operation": "try_previous",
                "status": "unknown",
                "code": "outcome_unknown",
                "operation_status": 500,
            },
        )
        raise WebChildOutcomeUnknown() from None
    if any(check["accepted"] for check in checks):
        _close(
            ws,
            held,
            "try_previous",
            {
                "operation": "try_previous",
                "status": "failed",
                "code": "previous_verified",
                "operation_status": 500,
                "checks": checks,
            },
        )
        raise WebChildOutcomeUnknown()
    first = checks[0]
    held["last_attempt"] = {
        "operation": "try_previous",
        "status": "refused",
        "code": first["code"],
        "operation_status": first["operation_status"],
        "checks": checks,
    }
    return _safe_snapshot(ws, held, "try_previous", "refused")


def sync_capability(ws, *, post: bool) -> None:
    """A closed ``lab`` capability revokes a running experiment (R2-E7); a read does it
    without calling the child's controller."""
    if FEATURE not in getattr(ws, "features", frozenset()) and SLOT in getattr(ws, "demos", {}):
        retire(ws, quiet=not post)


class LifecycleState:
    """Delegate state construction; retire children on every SessionStore exit path."""

    def __init__(self, wrapped):
        self.wrapped = wrapped

    def new_web_session(self, *args, **kwargs):
        return self.wrapped.new_web_session(*args, **kwargs)

    def reserve_pairs(self, count):
        self.wrapped.reserve_pairs(count)

    def retire_pair(self, ws):
        try:
            retire(ws)
        finally:
            self.wrapped.retire_pair(ws)

    def forget_pair(self, ws):
        self.wrapped.forget_pair(ws)
