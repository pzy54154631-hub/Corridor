"""Project domestic safety signals from the existing append-only audit log."""

from collections.abc import Iterable
from typing import Any

from corridor.stores.bank import add_seconds

MONEY_KINDS = frozenset({
    "transfer",
    "schedule_transfer",
    "lock_funds",
    "unlock_funds",
    "merchant_order",
    "refund_request",
    "purchase_product",
    "redeem_product",
})
SIGNALS = frozenset({"money_rejected", "money_accepted", "injection", "mfa_failed", "mfa_passed"})
BREAKER_REASONS = frozenset({"breaker_injection", "breaker_mfa"})


def project(
    records: Iterable[dict[str, Any]], *, principal: str, session: str | None, now: str
) -> dict[str, Any]:
    """Counters are recoverable without adding trusted-state fields or secrets."""
    money, mfa, status = 0, 0, "Normal"
    injections: list[str] = []
    for record in records:
        if record["principal_token"] != principal:
            continue
        payload = record["payload"]
        if record["event"] == "unlocked":
            money, mfa, status, injections = 0, 0, "Normal", []
        elif record["event"] == "safety_transition":
            status = payload["to"]
        elif record["event"] == "safety_signal":
            signal = payload["signal"]
            if signal == "money_rejected" and payload["device_session_id"] == session:
                money += 1
            elif signal == "money_accepted" and payload["device_session_id"] == session:
                money = 0
            elif signal == "injection":
                injections.append(record["ts"])
            elif signal == "mfa_failed":
                mfa += 1
            elif signal == "mfa_passed":
                mfa = 0
    return {
        "state": status,
        "consecutive_money_rejections": money,
        "injection_hits_10m": sum(add_seconds(now, -600) <= time <= now for time in injections),
        "consecutive_mfa_failures": mfa,
    }
