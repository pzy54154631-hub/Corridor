"""AppState: the whole page as data, rendered for the session's own locale and view.

Every entry point binds the session's locale and audience for the duration of the call
(the audience defaults to the judge view when unset, so a forgotten binding would send
judge evidence to a customer page). Judge-only keys are absent from the customer view,
not empty; the view is a display preference, never access control.
"""

from typing import Any

import corridor.web as web
from corridor.web import live, tasks
from corridor.web.catalog import bound, keys, template, wtr
from corridor.web.compat import display_names, tr
from corridor.web.vm.cards import judge
from corridor.web.vm.chat import PREFIX, chat_vm, notices
from corridor.web.vm.device import device_vm, phone_url
from corridor.web.vm.evidence import evidence_vm
from corridor.web.vm.text import plain

SHORTCUTS = ("transfer", "billing", "wealth", "cards", "subscriptions", "birthday", "scam_drill")
TABS = ("accounts", "wealth", "plans")
PAUSED = frozenset({"handoff", "handed_off", "paused"})
LOCKED = "Locked: yellow and red actions are blocked. Green queries remain available."
# Reused UI copy the shell may draw without a view model.
REUSED = (
    "simulation_notice",
    "bank_name",
    "Corridor assistant",
    "Cancel",
    "Confirm",
    "Stop",
    "Handoff",
    "Compensate",
    "Technical evidence",
    "polish.view.to_judge",
    "polish.view.to_customer",
    "polish.scam.other_gated",
    "polish.scam.other_gated_button",
    "polish.phone.autosubmit_hint",
    "Green · automatic query",
    "Yellow · device approval required",
    "Red · independent simulated MFA required",
    "Black · execution prohibited",
)


def capabilities_vm(features: Any = None) -> dict[str, Any]:
    """The build's FEATURES as capabilities; the page shows nothing they do not open."""
    found = web.FEATURES if features is None else features
    return {
        "tabs": [name for name in TABS if f"tab:{name}" in found],
        "shortcuts": [key for key in SHORTCUTS if f"shortcut:{key}" in found],
        "live": "live" in found,
        "plan_select": "plan_select" in found,
        "compensate": "compensate" in found,
        "aa": "aa" in found,
        "lab": "lab" in found,
    }


def session_vm(ws, snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "logged_in": bool(snapshot.get("logged_in")),
        "clock_text": wtr(
            "web.vm.session.clock", time=display_names.beijing(snapshot.get("clock"))[:16]
        ),
        "bank_name": plain(tr("bank_name")),
        "assistant_name": plain(tr("Corridor assistant")),
        "phone_url": phone_url(ws),
    }


def safety_vm(ws) -> dict[str, Any]:
    """The lock banner (P0-15): what is refused, what still works, and the way out."""
    safety = ws.safety or {}
    locked = safety.get("state") == "Locked"
    vm: dict[str, Any] = {
        "locked": locked,
        "text": plain(tr(LOCKED)) if locked else None,
        "hint": (
            wtr(
                "web.vm.safety.approval_rate"
                if safety.get("reason") == "approval_rate"
                else "web.vm.safety.way_out"
            )
            if locked
            else None
        ),
    }
    if judge():
        after = safety.get("unlock_after")
        vm.update({
            "state": str(safety.get("state") or "Normal"),
            "reason": safety.get("reason"),
            "unlock_after_text": (
                wtr("web.vm.session.clock", time=display_names.beijing(after)[:16])
                if after
                else None
            ),
            "rule_ids": list(safety.get("rule_ids") or []),
        })
    return vm


def shell_vm(ws, snapshot: dict[str, Any]) -> dict[str, Any]:
    vm: dict[str, Any] = {"corner": plain(live.corner(ws))}
    if getattr(ws, "public", False):
        # Public mode only (RW-7 item 4): the demo notice, on the first response and every
        # one after it, in both views and locales; the page draws it where it cannot be
        # closed. The key is absent outside public mode.
        vm["public_banner"] = plain(wtr("web.vm.public.banner"))
    if judge():
        vm["judge_strip"] = plain(live.judge_strip(ws))
    badge = live.badge(ws)
    vm.update({
        "badge": {"kind": badge.get("kind", "demo"), "text": plain(badge.get("text"))},
        "safety": safety_vm(ws),
        "paused_banner": (
            wtr("web.vm.session.untrusted")
            if ws.untrusted
            else plain(tr("Automatic execution is paused"))
            if snapshot.get("status") in PAUSED
            else None
        ),
        # One-shot notices of this request only: reading them consumes them.
        "notices": notices(ws, live.page_failure(ws), "n"),
    })
    return vm


def login_vm() -> dict[str, str]:
    return {
        "title": plain(tr("home_title")),
        "intro": plain(tr("home_intro")),
        "button": plain(tr("Enter synthetic demo")),
        "caption": plain(
            tr(
                "Fictional customers, institutions and products. Starting does not "
                "authorize payment."
            )
        ),
    }


def features_of(ws) -> Any:
    return getattr(ws, "features", None)


def app_state(ws) -> dict[str, Any]:
    """The AppState of one session (contract §4.1)."""
    with bound(ws):
        capabilities = capabilities_vm(features_of(ws))
        snapshot = ws.controller.snapshot()
        logged_in = bool(snapshot.get("logged_in"))
        current = ws.current.get(PREFIX) if logged_in else None
        state: dict[str, Any] = {
            "schema": web.SCHEMA_VERSION,
            "contract": web.CONTRACT,
            "generation": ws.generation,
            "revision": ws.revision,
            "view": ws.view,
            "locale": ws.locale,
            "capabilities": capabilities,
            "session": session_vm(ws, snapshot),
            "shell": shell_vm(ws, snapshot),
        }
        if not logged_in:
            state["login"] = login_vm()
        # Registered by the closing refresh of the last POST; listing them changes nothing.
        listed = tasks.collect(ws) if logged_in else []
        state["chat"] = chat_vm(
            ws,
            logged_in=logged_in,
            capabilities=capabilities,
            plans=snapshot.get("plans", []),
            listed=frozenset(task.key for task in listed),
        )
        state["device"] = device_vm(ws, listed=listed, current_rid=current, snapshot=snapshot)
        evidence = evidence_vm(ws, current_rid=current, snapshot=snapshot)
        if evidence is not None:
            state["evidence"] = evidence
        return state


def i18n_strings(ws) -> dict[str, str]:
    """Shell templates for this locale and view: ``web.ui.*`` plus a few reused UI keys.

    Templates keep their ``{name}`` placeholders; the page fills them as plain text.
    """
    with bound(ws):
        strings = {key: template(key) for key in keys("web.ui.")}
        strings.update({key: template(key) for key in REUSED})
        return strings
