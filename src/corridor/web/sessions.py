"""The session table: one WebSession per (browser, tab), bounded by count; by time only in
public mode.

The browser cookie and the server-issued tab locate a session. A full table evicts the
least recently used session with no request queued or running; when every candidate is
busy the new session is refused with 503. Eviction and reset revoke the session's phone
pairing, so an old ``/phone/<pair>`` link shows only the unpaired notice.

Public mode (``public`` given, with a clock) adds what an internet-facing table needs
(RW-8 items 1, 3, 4, 9): each session's last use is kept, refreshed by its own requests and
by its valid phone pairing's polls (the activity lease); ``reap`` retires sessions idle
longer than ``idle_seconds``; a full table evicts only a session idle longer than
``evict_idle_after`` and otherwise refuses the newcomer; one client address holds at most
``per_ip_sessions`` sessions, counted by the address that created each one and not moved
later; a retired pairing's inbox is forgotten in the outbox so churn never counts against
a session still in use; and the outbox is told to hold every pairing the table can have.
Outside public mode none of this bookkeeping runs and the table behaves as before.
"""

import logging
import os
import secrets
import threading
from collections import Counter, OrderedDict
from collections.abc import Callable
from typing import Any

from corridor.web import public as public_mode
from corridor.web.errors import error
from corridor.web.security import BROWSER, LOCALES, TAB, VIEWS

log = logging.getLogger("tornado.application")
Key = tuple[str, str]
# Public mode: outbox pairings per table slot. One for the session and one for the pairing
# of a reset or a build under way, which exists beside the session's own for a moment. The
# table bounds the pairings and forgets each one it retires, so the outbox's bound stays
# clear of every session in use (RW-9 item 1).
PAIRS_PER_SESSION = 2


def new_generation() -> str:
    return secrets.token_urlsafe(9)


def default_view(view: str | None) -> str:
    if view in VIEWS:
        return str(view)
    configured = os.environ.get("CORRIDOR_DEFAULT_VIEW", "customer")
    return configured if configured in VIEWS else "customer"


def default_locale(locale: str | None) -> str:
    if locale in LOCALES:
        return str(locale)
    configured = os.environ.get("CORRIDOR_LOCALE", "zh-CN")
    return configured if configured in LOCALES else "zh-CN"


class PairRegistry:
    """Pairings whose simulated phone may show messages; unknown and revoked look alike."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._allowed: set[str] = set()

    def allow(self, pair: str) -> None:
        with self._lock:
            self._allowed.add(pair)

    def revoke(self, pair: str) -> None:
        with self._lock:
            self._allowed.discard(pair)

    def allowed(self, pair: str) -> bool:
        with self._lock:
            return pair in self._allowed

    def __repr__(self) -> str:
        with self._lock:
            return f"PairRegistry(<{len(self._allowed)} allowed>)"


class SessionStore:
    def __init__(
        self,
        *,
        state: Any,
        live: Any,
        max_sessions: int = 8,
        max_tabs_per_browser: int = 4,
        public: Any = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if max_sessions < 1 or max_tabs_per_browser < 1:
            raise ValueError("The session table needs room for one session")
        if public is not None and clock is None:
            raise ValueError("Public mode needs a clock for its idle intervals")
        if public is not None:
            state.reserve_pairs(PAIRS_PER_SESSION * max_sessions)
        self.pairs = PairRegistry()
        self._state = state
        self._live = live
        self._max = max_sessions
        self._max_tabs = max_tabs_per_browser
        self._public = public
        self._clock = clock
        self._lock = threading.Lock()
        self._sessions: OrderedDict[Key, Any] = OrderedDict()
        self._reserved: Counter[str] = Counter()
        # Public mode only (all under the lock): each session's last use, the address that
        # created it and how many each address holds, the addresses of sessions being
        # built, and the session of each live pairing.
        self._seen: dict[Key, float] = {}
        self._client_of: dict[Key, str] = {}
        self._clients: Counter[str] = Counter()
        self._client_reserved: Counter[str] = Counter()
        self._by_pair: dict[str, Key] = {}

    @property
    def public(self) -> Any:
        return self._public

    def __len__(self) -> int:
        with self._lock:
            return len(self._sessions)

    def __repr__(self) -> str:
        return f"SessionStore(<{len(self)} sessions>)"

    def lookup(self, browser_id: str, tab: str) -> Any:
        """The session of this browser and tab (counts as a use), or None."""
        with self._lock:
            key = (browser_id, tab)
            ws = self._sessions.get(key)
            if ws is not None:
                self._sessions.move_to_end(key)
                self._touch(key)
            return ws

    def peek(self, browser_id: str, tab: str) -> Any:
        """Like ``lookup`` without touching the eviction order (tests and probes)."""
        with self._lock:
            return self._sessions.get((browser_id, tab))

    def current(self, ws: Any) -> bool:
        with self._lock:
            return self._sessions.get((ws.browser_id, ws.tab_id)) is ws

    def open(
        self,
        browser_id: str | None,
        tab: str | None,
        view: str | None,
        locale: str | None,
        *,
        hold: bool = False,
        client: str | None = None,
    ) -> tuple[Any, str, str, bool]:
        """Reuse (browser, tab) when it exists; otherwise a new session under a new tab.

        Tabs are issued here, never taken from the client. With ``hold`` a new session
        starts busy, so nothing evicts it before the caller's first response; the caller
        then calls ``release``. In public mode ``client`` is the creating address: a new
        session is refused with 503 while that address holds its share, before anyone
        else's session could be evicted for it.
        """
        browser_id = browser_id if browser_id and BROWSER.fullmatch(browser_id) else None
        with self._lock:
            if browser_id is not None and tab is not None and TAB.fullmatch(tab):
                ws = self._sessions.get((browser_id, tab))
                if ws is not None:
                    self._sessions.move_to_end((browser_id, tab))
                    self._touch((browser_id, tab))
                    return ws, browser_id, tab, False
            browser_id = browser_id or secrets.token_urlsafe(32)
            client = self._check_client(client)
            victims = self._make_room(browser_id)
            tab = secrets.token_urlsafe(24)
            while any(key[1] == tab for key in self._sessions):
                tab = secrets.token_urlsafe(24)
            # Reserved only once nothing below can refuse: a refusal leaves no count behind.
            self._reserved[browser_id] += 1
            if client is not None:
                self._client_reserved[client] += 1
        for victim in victims:
            self._retire(victim)
        try:
            ws = self._state.new_web_session(
                default_view(view), default_locale(locale), browser_id=browser_id, tab_id=tab
            )
            ws.generation = new_generation()
            ws.revision = 0
            if hold:
                ws.busy += 1
            self._start(ws)
            with self._lock:
                self._sessions[(browser_id, tab)] = ws
                self._register((browser_id, tab), ws, client)
        finally:
            with self._lock:
                self._reserved[browser_id] -= 1
                if self._reserved[browser_id] <= 0:
                    del self._reserved[browser_id]
                if client is not None:
                    self._client_reserved[client] -= 1
                    if self._client_reserved[client] <= 0:
                        del self._client_reserved[client]
        return ws, browser_id, tab, True

    def release(self, ws: Any) -> None:
        ws.busy -= 1

    def _start(self, ws: Any) -> None:
        """Open a built session's pairing and tell the live layer. A session that fails here
        never reaches the table, so its pairing goes back at once by the path every retired
        session takes, and the failure goes on to the caller (RW-9 item 6)."""
        public_mode.mark(ws, self._public)
        self.pairs.allow(ws.pair)
        try:
            self._live.on_new(ws)
        except Exception:
            self._retire(ws)
            raise

    def _check_client(self, client: str | None) -> str | None:
        """Public mode: the address's key, after refusing while it holds its share (the
        sessions it created plus those being built for it); lock held. None outside."""
        if self._public is None:
            return None
        client = client or public_mode.UNKNOWN
        held = self._clients[client] + self._client_reserved[client]
        if held >= self._public.per_ip_sessions:
            raise error("busy")
        return client

    def _register(self, key: Key, ws: Any, client: str | None) -> None:
        # Public mode's bookkeeping of a session just installed; lock held.
        if self._public is None:
            return
        self._seen[key] = self._clock()
        self._client_of[key] = client or public_mode.UNKNOWN
        self._clients[self._client_of[key]] += 1
        self._by_pair[ws.pair] = key

    def _touch(self, key: Key) -> None:
        # A use of the session, for the idle intervals; lock held.
        if self._public is not None:
            self._seen[key] = self._clock()

    def _drop(self, key: Key) -> Any:
        """Take a session out of the table with its bookkeeping; lock held."""
        ws = self._sessions.pop(key)
        if self._public is not None:
            self._seen.pop(key, None)
            client = self._client_of.pop(key, None)
            if client is not None:
                self._clients[client] -= 1
                if self._clients[client] <= 0:
                    del self._clients[client]
            if self._by_pair.get(ws.pair) == key:
                del self._by_pair[ws.pair]
        return ws

    def _make_room(self, browser_id: str) -> list[Any]:
        """Evict idle sessions, least recently used first, for one more; lock held.

        A browser past its tabs loses its own least recently used idle tab. A full table
        loses its least recently used idle session; in public mode only one idle longer than
        ``evict_idle_after``, so a newcomer never displaces a session in use (503 instead).
        """
        chosen: list[Key] = []
        at = self._clock() if self._public is not None else None

        def idle(candidates: list[Key], threshold: float | None) -> Key:
            for key in candidates:
                if key in chosen or self._sessions[key].busy:
                    continue
                if threshold is not None and at - self._seen.get(key, at) < threshold:
                    continue
                return key
            raise error("busy")

        same = [key for key in self._sessions if key[0] == browser_id]
        if len(same) + self._reserved[browser_id] >= self._max_tabs:
            chosen.append(idle(same, None))
        if len(self._sessions) - len(chosen) + sum(self._reserved.values()) >= self._max:
            threshold = self._public.evict_idle_after if self._public is not None else None
            chosen.append(idle(list(self._sessions), threshold))
        return [self._drop(key) for key in chosen]

    def reap(self) -> int:
        """Public mode: retire every session idle for ``idle_seconds`` with no request
        queued or running, by the same path as eviction; returns how many. A session's
        ``busy`` covers its queued and running jobs and a new session's first response, so
        no job is ever cut off. Outside public mode nothing is reaped."""
        if self._public is None:
            return 0
        at = self._clock()
        with self._lock:
            keys = [
                key
                for key, ws in self._sessions.items()
                if not ws.busy and at - self._seen.get(key, at) >= self._public.idle_seconds
            ]
            victims = [self._drop(key) for key in keys]
        for ws in victims:
            self._retire(ws)
        return len(victims)

    def touch_pair(self, pair: str) -> None:
        """Public mode: a poll of a pairing still in the table counts as a use of its
        session, the activity lease of a phone kept open (RW-8 item 3). A revoked or
        unknown pairing touches nothing."""
        if self._public is None:
            return
        with self._lock:
            key = self._by_pair.get(pair)
            if key is not None and key in self._sessions:
                self._sessions.move_to_end(key)
                self._seen[key] = self._clock()

    def reset(self, ws: Any) -> Any:
        """A new session in the same slot: new controllers, pairing and generation.

        The caller holds the old session's lock. View and locale carry over; the old
        pairing is revoked and its codes expire; the live session moves across. In public
        mode the old pairing's inbox is forgotten as well, on every branch.
        """
        new = self._state.new_web_session(
            ws.view, ws.locale, browser_id=ws.browser_id, tab_id=ws.tab_id
        )
        new.generation = new_generation()
        while new.generation == ws.generation:
            new.generation = new_generation()
        new.revision = 0
        self._start(new)
        with self._lock:
            key = (ws.browser_id, ws.tab_id)
            installed = self._sessions.get(key) is ws
            if installed:
                self._sessions[key] = new
                self._sessions.move_to_end(key)
                self._touch(key)
                if self._public is not None:
                    if self._by_pair.get(ws.pair) == key:
                        del self._by_pair[ws.pair]
                    self._by_pair[new.pair] = key
        if not installed:
            # Evicted meanwhile: nothing to replace, and the new pairing must not linger.
            self._retire(new)
            raise error("no_session")
        self.pairs.revoke(ws.pair)
        self._quietly(self._state.retire_pair, ws)
        self._forget(ws)
        self._quietly(lambda old: self._live.on_reset(old, new), ws)
        return new

    def close(self, ws: Any) -> None:
        """Forget a session: its pairing is revoked first, then its codes and live client."""
        with self._lock:
            key = (ws.browser_id, ws.tab_id)
            if self._sessions.get(key) is ws:
                self._drop(key)
        self._retire(ws)

    def _retire(self, ws: Any) -> None:
        self.pairs.revoke(ws.pair)
        self._quietly(self._state.retire_pair, ws)
        self._forget(ws)
        self._quietly(self._live.close, ws)

    def _forget(self, ws: Any) -> None:
        # Public mode only: the revoked pairing's inbox leaves the outbox (RW-8 item 4).
        if self._public is not None:
            self._quietly(self._state.forget_pair, ws)

    @staticmethod
    def _quietly(call: Callable[[Any], Any], ws: Any) -> None:
        # Cleanup goes on after a failure; the log names only the exception class.
        try:
            call(ws)
        except Exception as exc:
            log.error("session cleanup failed: %s", type(exc).__name__)
