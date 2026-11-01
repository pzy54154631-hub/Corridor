"""Public mode: configuration, client address, rate and in-flight limits, idle reaping.

Public mode is opened only by a public host name (``--public-host`` or the environment
variable ``CORRIDOR_WEB_PUBLIC_HOST``; RW-7 item 3). ``make_app`` then carries the frozen
``PublicConfig`` under ``settings["public"]`` and this process's counters, a ``Limits``,
under ``settings["limits"]``; without it both are None and nothing in this module runs
for a request, so the service answers as it did before (``tests/web/test_public_off.py``).

The process still binds a loopback address only. Caddy in front of it does TLS and sends
the client's address in ``X-Forwarded-For``; ``client_ip`` reads the right-hand end of
that header only (Caddy replaces, never appends, what an untrusted peer sent), folds an
IPv6 address to its /64 and files a missing or malformed value under one shared
``unknown`` key, which is stricter than letting it through. ``request.remote_ip`` is never
read (``SERVER_OPTIONS["xheaders"]`` stays False).

Limits (RW-8 items 1, 2, 7): token buckets per (scope, address) with a bounded table in
which a bucket that has not refilled is never dropped and new keys share one overflow
bucket while the table is full; at most a few requests in flight per address and per
browser, decided before a request takes a place in the shared executor; a cap on the
sessions one address holds, counted by the address that created each session and not
moved when the browser's address changes (RW-8 item 9); idle sessions reaped after
``idle_seconds`` and, when the table is full, only a session idle longer than
``evict_idle_after`` evicted for a newcomer, else 503 ``busy``.

Live access in public mode (RW-8 items 6, 8, 10, 11; design §4.7, §4.8): the server holds
no access code, only its PBKDF2 verifier ``LIVE_ACCESS_CODE`` next to the derivation's
public parameters ``CORRIDOR_WEB_ACCESS_KDF`` (``pbkdf2-sha256$<iterations>$<salt hex>``),
both written by ``python -m corridor.web.access_setup`` from a code that tool drew at
random. The browser derives the same verifier and proves it with the existing single-use
challenge, so the product layer's check is unchanged; on the wire the verifier is
equivalent to the code, which is why the pair must be present and well formed before a
public site with ``CORRIDOR_LIVE=1`` starts at all (``access_pair``), and why the live
path checks it again before every grant and client (``access_configured``). The spend is
bounded by the one fixed journal directory ``JOURNAL_PATH`` with ``LIVE_BUDGET_CNY`` at most
100 and ``LIVE_SESSION_CALLS`` at most 20 (``public_caps``); there is no daily rotation. A
public site never starts a ledger itself: ``python -m corridor.web.ledger_setup`` does, once,
so a lost ledger closes live access until an operator has reconciled the spend (R8 S2).

This is the one module of the web package that reads a clock: the monotonic clock, in
``now``, for refill and idle intervals only, never the wall clock and never for an
identifier. Everything else takes the clock as a value.

Logging here names counts and scopes only: never an address, a cookie, a tab, a pairing.
"""

import hashlib
import ipaddress
import logging
import re
import time
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any

from corridor.agent.llm import LLMConfigurationError
from corridor.ui.live_config import ACCESS_CODE, SWITCH, LiveConfig

log = logging.getLogger("tornado.application")

HOST_ENV = "CORRIDOR_WEB_PUBLIC_HOST"
DEPLOYMENTS_ENV = "CORRIDOR_WEB_PUBLIC_DEPLOYMENTS"
# The verifier's derivation parameters, next to the verifier itself (``LIVE_ACCESS_CODE``,
# the product's name for what it compares against; in public mode that is the verifier).
KDF_ENV = "CORRIDOR_WEB_ACCESS_KDF"
VERIFIER_ENV = ACCESS_CODE
LIVE_ENV = SWITCH
# A lower-case DNS name without port, path or trailing dot (design §3.1); an address
# literal (an all-digit last label) is not one.
HOST = re.compile(r"[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?)+")
# Fixed CNY-only site allow list (RW-12); the experiment's six rows remain frozen.
DEPLOYMENTS = frozenset({
    "deepseek-flash",
    "deepseek-v4-pro",
    "zhipu-glm-5.2",
    "zhipu-glm-5.1",
    "zhipu-glm-5-turbo",
    "zhipu-glm-4.7",
})
UNKNOWN = "unknown"
RETRY_AFTER = "5"
FORWARDED_FOR = "X-Forwarded-For"
# Public-only options of the entry point: given without a public host, the entry refuses.
PUBLIC_ONLY = ("idle_seconds", "evict_idle_after", "per_ip_sessions", "per_ip_new_sessions")
# The verifier: PBKDF2-HMAC-SHA256 of the code, 256 bits as lower-case hex. The browser
# derives it with WebCrypto (static/js/kdf.js) and the tool with hashlib; the iterations
# are bounded so a configuration cannot make the browser's derivation trivial or endless.
KDF_ALGORITHM = "pbkdf2-sha256"
KDF_ITERATIONS = 200_000
KDF_MIN_ITERATIONS = 100_000
KDF_MAX_ITERATIONS = 2_000_000
KDF_SALT_BYTES = 16
SALT = re.compile(rf"[0-9a-f]{{{2 * KDF_SALT_BYTES}}}")
VERIFIER = re.compile(r"[0-9a-f]{64}")
# The public site's spend bounds (RW-8 item 10): one fixed journal directory, a cumulative
# budget of at most this many yuan and at most this many calls per session. The directory
# is a constant here, not a setting: a new directory would be a new budget (R8 S2).
JOURNAL_PATH = Path("/var/lib/corridor/journal")
BUDGET_CNY = Fraction(100)
SESSION_CALLS = 20
# The frozen client's SDK and HTTP libraries log every request body at DEBUG and the
# endpoint at INFO; a public site keeps their warnings only, so no judge's words can reach
# the process journal through them whatever the root level is (design §4.11).
CLIENT_LOGGERS = ("openai", "httpx", "httpcore")


def quiet_client_logs() -> None:
    for name in CLIENT_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def now() -> float:
    """Seconds on the monotonic clock: the one clock the web package reads."""
    return time.monotonic()


Clock = Callable[[], float]


@dataclass(frozen=True)
class Rate:
    """A token bucket's shape: ``burst`` tokens, one back every ``interval`` seconds."""

    burst: int
    interval: float

    def __post_init__(self) -> None:
        if self.burst < 1 or self.interval <= 0:
            raise ValueError("a rate needs a positive burst and interval")

    @classmethod
    def parse(cls, text: str) -> "Rate":
        """``COUNT/SECONDS``: ``count`` tokens spread over ``seconds`` (``30/600``)."""
        count, slash, seconds = text.partition("/")
        if not slash or not count.isdigit() or not seconds.isdigit():
            raise ValueError("a rate is written COUNT/SECONDS, for example 30/600")
        if int(count) < 1 or int(seconds) < 1:
            raise ValueError("a rate is written COUNT/SECONDS, for example 30/600")
        return cls(int(count), int(seconds) / int(count))


# Scopes and their defaults (design §4.6, lowered as RW-8 item 2 asks; item 1 for session).
RATES: Mapping[str, Rate] = {
    # New sessions per address: 30 in ten minutes (RW-8 item 1).
    "session": Rate(30, 20.0),
    # Access-code attempts per address: five, one back every two minutes (design §4.6).
    "unlock": Rate(5, 120.0),
    # Session API calls per address: a burst of 120, then two a second.
    "api": Rate(120, 0.5),
    # Phone inbox polls per address, in their own bucket (RW-8 item 2): twelve back a second,
    # so ten phones behind one address, each polling once a second (static/js/views/sms.js),
    # keep their burst (RW-9 item 5).
    "phone": Rate(120, 1 / 12),
    # Pages and static files per address: a burst of 1200, sixty back a second. The first
    # screen alone is 46 files, so ten browsers behind one address need about 460 at once
    # (RW-10 item 1). These reads take no executor place and touch no session.
    "page": Rate(1200, 1 / 60),
}


@dataclass(frozen=True)
class Kdf:
    """The verifier's public derivation parameters: PBKDF2-HMAC-SHA256, 256 bits.

    The text form ``pbkdf2-sha256$<iterations>$<salt hex>`` is what the configuration
    holds and the tool prints; ``wire`` is what ``/api/live/challenge`` sends the browser.
    Neither is a secret: the verifier is as strong as the code it was derived from.
    """

    iterations: int = KDF_ITERATIONS
    salt: str = ""

    def __post_init__(self) -> None:
        if (
            type(self.iterations) is not int
            or not KDF_MIN_ITERATIONS <= self.iterations <= KDF_MAX_ITERATIONS
        ):
            raise ValueError(
                f"{KDF_ENV} iterations must be between "
                f"{KDF_MIN_ITERATIONS} and {KDF_MAX_ITERATIONS}"
            )
        if not isinstance(self.salt, str) or not SALT.fullmatch(self.salt):
            raise ValueError(
                f"{KDF_ENV} needs a salt of {2 * KDF_SALT_BYTES} lower-case hex digits"
            )

    @classmethod
    def parse(cls, text: str) -> "Kdf":
        algorithm, _, rest = text.strip().partition("$")
        iterations, _, salt = rest.partition("$")
        if algorithm != KDF_ALGORITHM or not iterations.isdigit():
            raise ValueError(
                f"{KDF_ENV} must read {KDF_ALGORITHM}$<iterations>$<salt hex>, as printed by "
                "python -m corridor.web.access_setup"
            )
        return cls(int(iterations), salt)

    def text(self) -> str:
        return f"{KDF_ALGORITHM}${self.iterations}${self.salt}"

    def wire(self) -> dict[str, Any]:
        return {"algorithm": KDF_ALGORITHM, "iterations": self.iterations, "salt": self.salt}

    def verifier(self, code: str) -> str:
        """The verifier of ``code``: what the browser derives and the configuration holds."""
        return hashlib.pbkdf2_hmac(
            "sha256", code.encode("utf-8"), bytes.fromhex(self.salt), self.iterations, 32
        ).hex()


def access_pair(environ: Mapping[str, str]) -> Kdf | None:
    """The configured KDF when the verifier pair is present and well formed; None when
    neither is set. One without the other, a malformed KDF, or a ``LIVE_ACCESS_CODE`` that
    is not a 64-digit verifier (a plain code, say) raises ``ValueError`` (RW-8 item 11)."""
    kdf_text = str(environ.get(KDF_ENV, "")).strip()
    verifier = str(environ.get(VERIFIER_ENV, "")).strip()
    if not kdf_text and not verifier:
        return None
    if not kdf_text or not verifier:
        raise ValueError(
            f"{KDF_ENV} and {VERIFIER_ENV} are set together or not at all "
            "(python -m corridor.web.access_setup prints both)"
        )
    kdf = Kdf.parse(kdf_text)
    if not VERIFIER.fullmatch(verifier):
        raise ValueError(
            f"{VERIFIER_ENV} must be the 64-digit verifier printed by "
            "python -m corridor.web.access_setup, never a code"
        )
    return kdf


def access_configured(environ: Mapping[str, str]) -> bool:
    """Whether the verifier pair in ``environ`` is present and well formed right now: the
    live path's own check before a grant or a client (RW-8 item 11), read and dropped."""
    try:
        return access_pair(environ) is not None
    except ValueError:
        return False


def live_switched_on(environ: Mapping[str, str]) -> bool:
    return str(environ.get(LIVE_ENV, "")).strip() == "1"


def public_caps(config: LiveConfig) -> str | None:
    """Why a live configuration exceeds the public site's bounds (RW-8 item 10), or None:
    the journal directory is ``JOURNAL_PATH``, the budget is at most ``BUDGET_CNY`` and a
    session makes at most ``SESSION_CALLS`` calls. Reads the parsed configuration only."""
    if config.journal_dir != JOURNAL_PATH:
        return f"LIVE_JOURNAL_DIR must be the one fixed journal directory {JOURNAL_PATH}"
    if not 1 <= config.session_calls <= SESSION_CALLS:
        return f"LIVE_SESSION_CALLS must be between 1 and {SESSION_CALLS}"
    if not 0 < config.budget_cny <= BUDGET_CNY:
        return f"LIVE_BUDGET_CNY must be above 0 and at most {BUDGET_CNY}"
    return None


def live_configuration(environ: Mapping[str, str]) -> LiveConfig:
    """The product's live configuration of ``environ``; a value it cannot parse is a
    ``ValueError`` with the message for ``parser.error``."""
    try:
        return LiveConfig.from_environ(environ)
    except LLMConfigurationError:
        raise ValueError("the live caps (LIVE_SESSION_CALLS, LIVE_BUDGET_*) do not parse") from None


@dataclass(frozen=True)
class PublicConfig:
    """The public deployment's settings; ``from_args`` is the only way the entry builds one.

    Capacities are RW-8 item 1: 128 sessions, idle reaping after an hour, eviction of a
    full table only past half an hour idle, 16 sessions and 30 new sessions in ten minutes
    per address. In-flight limits are RW-8 item 2 as RW-10 item 3 raised them: twelve per
    address, so ten browsers behind one exit can act at once, three per browser.
    ``kdf`` is the verifier pair's derivation when the pair is configured (RW-8 item 11);
    with the live switch on, ``from_args`` requires it and the public spend caps.
    """

    host: str
    max_sessions: int = 128
    idle_seconds: float = 3600.0
    evict_idle_after: float = 1800.0
    per_ip_sessions: int = 16
    inflight_per_source: int = 12
    inflight_per_browser: int = 3
    rates: Mapping[str, Rate] = field(default_factory=lambda: dict(RATES))
    table_size: int = 4096
    deployments: frozenset[str] = DEPLOYMENTS
    kdf: Kdf | None = None

    def __post_init__(self) -> None:
        if not HOST.fullmatch(self.host) or self.host.rpartition(".")[2].isdigit():
            raise ValueError("--public-host must be a lower-case DNS name without a port")
        if self.max_sessions < 1 or self.per_ip_sessions < 1 or self.table_size < 1:
            raise ValueError("public mode needs room for one session and one bucket")
        if self.idle_seconds <= 0 or self.evict_idle_after <= 0:
            raise ValueError("--idle-seconds and --evict-idle-after must be positive")
        if self.evict_idle_after > self.idle_seconds:
            raise ValueError("--evict-idle-after cannot exceed --idle-seconds")
        if self.inflight_per_source < 1 or self.inflight_per_browser < 1:
            raise ValueError("public mode needs one request in flight per source and browser")
        if set(self.rates) != set(RATES):
            raise ValueError("public mode rates must name exactly the known scopes")
        from corridor.web.compat import SITE_DEPLOYMENTS as site_deployments

        currencies = {item.id: item.currency for item in site_deployments}
        if (
            not self.deployments
            or not self.deployments <= DEPLOYMENTS
            or "deepseek-flash" not in self.deployments
            or any(currencies.get(item) != "CNY" for item in self.deployments)
        ):
            raise ValueError(
                f"{DEPLOYMENTS_ENV} must contain deepseek-flash and only allowed CNY deployments"
            )
        if self.kdf is not None and not isinstance(self.kdf, Kdf):
            raise ValueError("public mode's kdf is a Kdf or None")

    @classmethod
    def from_args(cls, args: Any, environ: Mapping[str, str]) -> "PublicConfig | None":
        """The configuration of the entry's arguments, or None outside public mode.

        Without a public host every public-only option is refused, so a deployment cannot
        believe it is public when it is not; with one the deployment list must be an allowed
        CNY subset containing ``deepseek-flash`` (RW-12), the verifier pair must be whole
        or absent, and with
        ``CORRIDOR_LIVE=1`` the pair must be present and the live caps within the public
        bounds (RW-8 items 6, 10, 11), else the entry refuses to start. Raises
        ``ValueError`` with the message for ``parser.error``.
        """
        host = getattr(args, "public_host", None) or environ.get(HOST_ENV) or None
        given = [name for name in PUBLIC_ONLY if getattr(args, name, None) is not None]
        if host is None:
            if given:
                names = ", ".join("--" + name.replace("_", "-") for name in given)
                raise ValueError(f"{names} only apply with --public-host")
            return None
        values: dict[str, Any] = {"host": host}
        if getattr(args, "max_sessions", None) is not None:
            values["max_sessions"] = args.max_sessions
        for name in ("idle_seconds", "evict_idle_after", "per_ip_sessions"):
            value = getattr(args, name, None)
            if value is not None:
                values[name] = value
        rates = dict(RATES)
        new_sessions = getattr(args, "per_ip_new_sessions", None)
        if new_sessions is not None:
            rates["session"] = Rate.parse(new_sessions)
        values["rates"] = rates
        listed = environ.get(DEPLOYMENTS_ENV)
        if listed is not None:
            values["deployments"] = frozenset(
                item.strip() for item in listed.split(",") if item.strip()
            )
        values["kdf"] = access_pair(environ)
        if live_switched_on(environ):
            if values["kdf"] is None:
                raise ValueError(
                    f"public mode with {LIVE_ENV}=1 needs {KDF_ENV} and {VERIFIER_ENV} "
                    "(python -m corridor.web.access_setup prints both)"
                )
            exceeded = public_caps(live_configuration(environ))
            if exceeded is not None:
                raise ValueError(exceeded)
        return cls(**values)


def client_ip(headers: Any) -> str:
    """The limiter's key for this request: the right-most forwarded address.

    IPv4 is the address itself; IPv6 is its /64 network. A missing header, an address with
    a port or brackets, or anything that is not an address is ``unknown``: one shared key,
    so a request without a usable address is limited harder, not let through.
    """
    lines = headers.get_list(FORWARDED_FOR)
    if not lines:
        return UNKNOWN
    last = lines[-1].rpartition(",")[2].strip()
    try:
        address = ipaddress.ip_address(last)
    except ValueError:
        return UNKNOWN
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        address = mapped
    if address.version == 6:
        return str(ipaddress.ip_network((address, 64), strict=False))
    return str(address)


@dataclass
class Bucket:
    tokens: float
    updated: float

    def refill(self, rate: Rate, at: float) -> None:
        if at > self.updated:
            self.tokens = min(float(rate.burst), self.tokens + (at - self.updated) / rate.interval)
        self.updated = at

    def full(self, rate: Rate, at: float) -> bool:
        return self.tokens + max(0.0, at - self.updated) / rate.interval >= rate.burst


class RateLimiter:
    """Token buckets keyed by (scope, address), called on the IO loop thread only.

    Each scope has a table of at most ``table_size`` buckets. A bucket that has refilled
    completely is the same as no bucket, so it may be dropped to make room; a bucket that
    has not is never dropped (RW-8 item 7: no reset by eviction). While a table is full of
    unrefilled buckets, every key not already in it shares the scope's one overflow bucket.
    """

    def __init__(self, rates: Mapping[str, Rate], *, table_size: int = 4096, clock: Clock = now):
        if table_size < 1:
            raise ValueError("the limiter needs room for one bucket")
        self._rates = dict(rates)
        self._size = table_size
        self._clock = clock
        self._tables: dict[str, dict[str, Bucket]] = {scope: {} for scope in rates}
        self._overflow: dict[str, Bucket] = {}
        self.refused: Counter[str] = Counter()

    def allow(self, scope: str, key: str) -> bool:
        rate = self._rates[scope]
        at = self._clock()
        bucket = self._bucket(scope, key, rate, at)
        bucket.refill(rate, at)
        if bucket.tokens < 1:
            self.refused[scope] += 1
            return False
        bucket.tokens -= 1
        return True

    def _bucket(self, scope: str, key: str, rate: Rate, at: float) -> Bucket:
        table = self._tables[scope]
        bucket = table.get(key)
        if bucket is not None:
            return bucket
        if len(table) >= self._size:
            for held in [name for name, item in table.items() if item.full(rate, at)]:
                del table[held]
        if len(table) >= self._size:
            overflow = self._overflow.get(scope)
            if overflow is None:
                overflow = self._overflow[scope] = Bucket(float(rate.burst), at)
            return overflow
        bucket = table[key] = Bucket(float(rate.burst), at)
        return bucket

    def tokens(self, scope: str, key: str) -> float | None:
        """The bucket's tokens as of its last use (tests and probes); None when it has none."""
        bucket = self._tables[scope].get(key)
        return None if bucket is None else bucket.tokens

    def size(self, scope: str) -> int:
        return len(self._tables[scope])

    def drain_refused(self) -> dict[str, int]:
        counts = {scope: count for scope, count in sorted(self.refused.items()) if count}
        self.refused.clear()
        return counts


class Inflight:
    """Requests admitted but not yet answered, per address and per browser.

    Decided on the IO loop thread before a request takes a place in the executor's queue
    (RW-8 item 2): one address holds at most ``per_source`` places and one browser cookie at
    most ``per_browser``, whatever the pool. A refused request takes no place at all.
    """

    def __init__(self, per_source: int, per_browser: int) -> None:
        self._per_source = per_source
        self._per_browser = per_browser
        self.sources: Counter[str] = Counter()
        self.browsers: Counter[str] = Counter()

    def acquire(self, source: str, browser: str | None) -> tuple[str, str | None] | None:
        if self.sources[source] >= self._per_source:
            return None
        if browser is not None and self.browsers[browser] >= self._per_browser:
            return None
        self.sources[source] += 1
        if browser is not None:
            self.browsers[browser] += 1
        return source, browser

    def release(self, held: tuple[str, str | None]) -> None:
        source, browser = held
        self._decrement(self.sources, source)
        if browser is not None:
            self._decrement(self.browsers, browser)

    @staticmethod
    def _decrement(counter: Counter[str], key: str) -> None:
        counter[key] -= 1
        if counter[key] <= 0:
            del counter[key]


class Limits:
    """One process's limiter and in-flight counters, built by ``make_app`` in public mode."""

    def __init__(self, config: PublicConfig, *, clock: Clock = now) -> None:
        self.config = config
        self.limiter = RateLimiter(config.rates, table_size=config.table_size, clock=clock)
        self.inflight = Inflight(config.inflight_per_source, config.inflight_per_browser)


def scopes(path: str) -> tuple[str, ...]:
    """The buckets a request's path draws from: the phone its own, the access-code unlock
    both the API's and its own, any other API path the API's, everything else the page's."""
    if path.startswith("/api/phone/"):
        return ("phone",)
    if path == "/api/live/unlock":
        return ("api", "unlock")
    if path.startswith("/api/"):
        return ("api",)
    return ("page",)


def mark(ws: Any, config: PublicConfig | None) -> None:
    """Set a session's public-mode flags when it is built or rebuilt (RW-8 item 5).

    Outside public mode nothing is written; the session's fields keep their defaults.
    """
    if config is None:
        return
    ws.public = True
    ws.public_deployments = config.deployments


def tick(app: Any) -> int:
    """One periodic pass (the entry runs it every minute): reap idle sessions and log
    counts. A failure is logged by its class name; the next pass runs regardless."""
    store = app.settings["store"]
    limits = app.settings.get("limits")
    try:
        reaped = store.reap()
    except Exception as exc:
        log.error("public reap failed: %s", type(exc).__name__)
        reaped = 0
    if reaped:
        log.info("public reaped %d", reaped)
    refused = limits.limiter.drain_refused() if limits is not None else {}
    if refused:
        log.info("public refused %s", " ".join(f"{scope}={n}" for scope, n in refused.items()))
    return reaped
