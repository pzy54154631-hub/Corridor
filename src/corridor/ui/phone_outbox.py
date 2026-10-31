"""Process-local outbox of immutable synthetic SMS copies for the simulated phone page.

The banking session writes one immutable copy per challenge; the phone page (a
separate browser session) only reads it. Pairing tokens separate demo sessions;
they are a demo channel, not a security boundary. Nothing here touches a
controller, gateway, verifier or audit, and nothing consults the wall clock.
"""

from collections import OrderedDict
from dataclasses import dataclass, field, replace
from threading import Lock

from corridor.crypto.jcs import H

MAX_MESSAGES = 20
MAX_PAIRS = 64
STATUSES = ("active", "used", "expired")


LocalizedFields = tuple[tuple[str, tuple[tuple[str, str], ...]], ...]


@dataclass(frozen=True)
class SimulatedSMS:
    ref: str
    kind: str
    template: str
    fields: LocalizedFields = field(default_factory=tuple)
    code: str = ""
    expiry: str = ""
    status: str = "active"
    notes: tuple[str, ...] = ()

    def values(self, locale: str) -> dict[str, str]:
        """Display values of one locale; values are text only, never authorization input."""
        return dict(dict(self.fields).get(locale, ()))


def sms_ref(challenge_id: str) -> str:
    """A display reference derived from the challenge; the challenge id is never stored."""
    return H(challenge_id)[:16]


class Outbox:
    def __init__(self) -> None:
        self._lock = Lock()
        self._pairs: OrderedDict[str, OrderedDict[str, SimulatedSMS]] = OrderedDict()
        self._max_pairs = MAX_PAIRS

    @property
    def max_pairs(self) -> int:
        with self._lock:
            return self._max_pairs

    def reserve(self, pairs: int) -> None:
        """Hold at least ``pairs`` pairings from now on; the bound only ever rises.

        Only the web session table of a public site calls this (RW-9 item 1): it holds more
        sessions than ``MAX_PAIRS`` and forgets every pairing it retires, so the bound must
        never drop a pairing still in use. Every other caller keeps ``MAX_PAIRS``.
        """
        with self._lock:
            self._max_pairs = max(self._max_pairs, pairs)

    def deliver(self, pair: str, sms: SimulatedSMS) -> SimulatedSMS:
        """Idempotent by reference: a repeated delivery returns the stored copy."""
        if not pair or not isinstance(sms, SimulatedSMS) or sms.status not in STATUSES:
            raise ValueError("Invalid simulated SMS delivery")
        with self._lock:
            inbox = self._pairs.get(pair)
            if inbox is None:
                inbox = self._pairs[pair] = OrderedDict()
                while len(self._pairs) > self._max_pairs:
                    self._pairs.popitem(last=False)
            existing = inbox.get(sms.ref)
            if existing is not None:
                return existing
            inbox[sms.ref] = sms
            while len(inbox) > MAX_MESSAGES:
                inbox.popitem(last=False)
            return sms

    def inbox(self, pair: str) -> tuple[SimulatedSMS, ...]:
        """Newest first; an unknown pairing has an empty inbox."""
        with self._lock:
            return tuple(reversed(tuple((self._pairs.get(pair) or {}).values())))

    def register(self, pair: str) -> None:
        """Open an empty inbox so the phone page can show "no messages yet"."""
        if not pair:
            raise ValueError("Invalid simulated phone pairing")
        with self._lock:
            if pair not in self._pairs:
                self._pairs[pair] = OrderedDict()
                while len(self._pairs) > self._max_pairs:
                    self._pairs.popitem(last=False)

    def known(self, pair: str) -> bool:
        with self._lock:
            return pair in self._pairs

    def forget(self, pair: str) -> None:
        """Drop a pairing's inbox, so a retired pairing never counts against the bound.

        An unknown pairing is left as it is. The copies themselves are immutable and no
        controller, verifier or audit is touched; the web session table calls this after
        it has revoked the pairing and expired its codes.
        """
        with self._lock:
            self._pairs.pop(pair, None)

    def mark(self, pair: str, ref: str, status: str) -> None:
        """Retire a copy as used or expired; a retired copy never shows its code again."""
        if status not in STATUSES:
            raise ValueError("Unknown simulated SMS status")
        with self._lock:
            inbox = self._pairs.get(pair)
            if inbox is None or ref not in inbox or inbox[ref].status != "active":
                return
            inbox[ref] = replace(inbox[ref], status=status, code="")

    def clear(self) -> None:
        with self._lock:
            self._pairs.clear()


OUTBOX = Outbox()
