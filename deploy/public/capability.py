"""Refuse a release without the public-mode abilities (RW-8 item 18).

``check_release.sh`` runs this with the release's own interpreter before a release may
serve, on install and on rollback alike. It reads the release's code only: no environment
file, no credential, no ledger. Bounds are upper bounds, so a stricter release still passes.
Exit status 0 means every ability is present; 1 names what is missing.
"""

import inspect
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

MAX_SESSIONS = 128
PER_IP_SESSIONS = 16
INFLIGHT_PER_SOURCE = 12
INFLIGHT_PER_BROWSER = 3
BUDGET_CNY = 100
SESSION_CALLS = 20
PROBE_HOST = "probe.example.test"
JOURNAL_PATH = Path("/var/lib/corridor/journal")


def _config() -> Any:
    from corridor.web.public import PublicConfig

    return PublicConfig(host=PROBE_HOST)


# The six CNY models of RW-12; a release with any other list is refused.
SITE_MODELS = frozenset({
    "deepseek-flash",
    "deepseek-v4-pro",
    "zhipu-glm-5.2",
    "zhipu-glm-5.1",
    "zhipu-glm-5-turbo",
    "zhipu-glm-4.7",
})


def _deployment_is_exact() -> bool:
    from corridor.web import public

    return public.DEPLOYMENTS == SITE_MODELS


def _spend_is_bounded() -> bool:
    from corridor.web import public

    return public.BUDGET_CNY <= BUDGET_CNY and public.SESSION_CALLS <= SESSION_CALLS


def _sessions_are_bounded() -> bool:
    config = _config()
    return config.max_sessions <= MAX_SESSIONS and config.per_ip_sessions <= PER_IP_SESSIONS


def _inflight_is_bounded() -> bool:
    config = _config()
    return (
        config.inflight_per_source <= INFLIGHT_PER_SOURCE
        and config.inflight_per_browser <= INFLIGHT_PER_BROWSER
    )


def _cookie_is_host_bound() -> bool:
    from corridor.web import security

    return security.cookie_name(_config()).startswith("__Host-")


def _verifier_pair_is_required() -> bool:
    from corridor.web import public

    try:
        public.access_pair({public.VERIFIER_ENV: "a-plain-code"})
    except ValueError:
        return True
    return False


def _shared_lock_can_be_closed() -> bool:
    from corridor.ui.live_runtime import LiveSession

    return "global_lock" in inspect.signature(LiveSession.__init__).parameters


def _retired_pairs_are_forgotten() -> bool:
    from corridor.ui.phone_outbox import Outbox

    return callable(getattr(Outbox, "forget", None))


def _outbox_holds_the_table() -> bool:
    from corridor.ui.phone_outbox import Outbox
    from corridor.web.sessions import PAIRS_PER_SESSION

    outbox = Outbox()
    outbox.reserve(PAIRS_PER_SESSION * MAX_SESSIONS)
    for index in range(MAX_SESSIONS):
        outbox.register(f"pair-{index}")
    return outbox.known("pair-0")


def _journal_is_fixed() -> bool:
    from corridor.web import public

    return public.JOURNAL_PATH == JOURNAL_PATH


def _ledger_is_never_started_by_the_site() -> bool:
    from corridor.ui.live_runtime import LiveSession, SpendLedger
    from corridor.web import ledger_setup

    return (
        "create" in inspect.signature(SpendLedger.__init__).parameters
        and "ledger_create" in inspect.signature(LiveSession.__init__).parameters
        and callable(ledger_setup.main)
    )


def _access_tool_is_present() -> bool:
    from corridor.web import access_setup

    return callable(access_setup.main)


CHECKS: dict[str, Callable[[], bool]] = {
    "the ruled site models": _deployment_is_exact,
    "cumulative spend caps": _spend_is_bounded,
    "session table caps": _sessions_are_bounded,
    "in-flight caps": _inflight_is_bounded,
    "__Host- cookie": _cookie_is_host_bound,
    "verifier pair": _verifier_pair_is_required,
    "no shared wrong-code lock": _shared_lock_can_be_closed,
    "outbox forget": _retired_pairs_are_forgotten,
    "outbox holds the table": _outbox_holds_the_table,
    "fixed journal directory": _journal_is_fixed,
    "no ledger started by the site": _ledger_is_never_started_by_the_site,
    "access-code tool": _access_tool_is_present,
}


def missing() -> list[str]:
    absent = []
    for name, check in CHECKS.items():
        try:
            present = check()
        except Exception:
            # A release too old to import the check lacks the ability.
            present = False
        if not present:
            absent.append(name)
    return absent


def main() -> int:
    absent = missing()
    if absent:
        sys.stderr.write("missing public-mode abilities: " + ", ".join(absent) + "\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
