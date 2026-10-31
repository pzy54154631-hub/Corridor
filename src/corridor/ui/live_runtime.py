"""Session-only live access, two spending caps and a replayable journal (product layer).

``LiveSession`` lives in one Streamlit session: whether access was granted, which
deployment is selected, the attempt and spend counters and the outcome of every call
attempt. It builds a ``JournalClient``: the frozen ``LLMClient`` in record mode, so every
real call is written to the journal directory and replays strictly offline, plus two caps
checked before each call: calls per session, and spend per journal directory and
currency, cumulative and shared across sessions and processes through a small SQLite
ledger kept in that directory. The ledger has no calendar and this module reads no host
clock: the spend total starts again only when the operator points ``LIVE_JOURNAL_DIR`` at
a new directory or raises the cap. Each journal directory also holds a manifest (the
deployment, its currency and price basis) and an outcome index (one line per attempt:
recorded, refused by the agent's checks, or failed, and how its cost was settled).

Secrets (S7 external review 3). The access code is never typed into a Streamlit widget:
the page's access field hashes it in the browser with a single-use challenge (HMAC-SHA256)
and only that proof reaches the server, where it is compared with a proof computed from
the code read from the environment or the credential file at that moment. The API key is
read from the credential file by path at each call, set on the client for that call only
and cleared when it returns, so between calls no session object holds a key or a code.
A grant records a keyed fingerprint (a per-process random salt, never stored) of the code
source and of every credential file: a rotated code or key revokes the grant at the next
call. Nothing here touches the kernel, the gateway, the synthetic clock or the audit chain.

C3 (review of round 3). A configured access code shorter than 20 characters or with fewer
than 8 different ones makes live access unavailable (the proof the page keeps would let
anyone holding it recover a short code offline). Wrong codes from other sessions block new
grants once 20 accumulate, never a grant already made; a host that generates the code at
random with at least 128 bits of entropy and limits attempts by client address may build
its sessions with ``global_lock=False`` (RW-8 item 8: on the internet that shared lock is a
cheap denial of service), which keeps the per-session lock and still records every wrong
code on the ledger. Every call's bookkeeping runs in a
guarded block: a settlement or record that fails becomes a fault record and the call is
released exactly once, whatever happens. Provider usage above the reservation together
with a failed settlement leaves the ledger "to be reconciled": no session reserves until
the operator clears it. The error a caller sees is a fresh product-layer error with no
cause, context, argument or frame that could hold the key. Each call reads its
configuration and its credential files once and uses that one reading for the checks, the
caps and the key. A ledger or journal directory, or any of its parents, that is a
symbolic link, a junction or another reparse point is refused. These are guards under
assumed prices, not a provider's billing limit, and "no key between calls" is what the
sentinel scans of the tested outcomes show, not a promise that memory is wiped.
"""

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import stat
import sys
from collections.abc import Callable, Mapping
from fractions import Fraction
from pathlib import Path
from threading import Lock, RLock
from typing import Any

from corridor.agent.llm import _ENV_FIELDS, BudgetExceeded, LLMClient, LLMConfigurationError
from corridor.schemas.money import fraction_string
from corridor.ui.live_config import (
    CURRENCIES,
    DEPLOYMENTS,
    Credential,
    Deployment,
    LiveConfig,
    access_code_strong,
    deployment,
    environ_access_code,
    parse_credential,
    read_credential,
)

LOCK_AFTER_FAILURES = 5
# Wrong access codes across every session sharing one ledger block new grants for all of
# them; there is no cooldown (that would need a clock), only an operator reset. C3: a grant
# already made is never revoked by other sessions' wrong codes (no shared denial of
# service); rate limiting belongs to the reverse proxy in front of the page.
GLOBAL_LOCK_AFTER = 20
# Each call reserves the assumed cost of this many input tokens plus the deployment's
# output cap. The frozen client computes its own bound for the request it is about to
# send (the UTF-8 bytes of the whole request body plus 1024 tokens of framing, and the
# output cap); for each call ``JournalClient`` lowers the client's spend cap to exactly
# this reservation, so the frozen client itself refuses before sending any request whose
# bound exceeds what the ledger reserved. 16384 covers a request of up to about 4000
# characters of at most 3 UTF-8 bytes each, plus about 2.2 KB of prompt and schema. C3: a
# request with characters of 4 UTF-8 bytes (extension-plane CJK, emoji) or a longer one
# can exceed it; the frozen client then refuses it before sending (budget-rejected), and
# the page shows a failure: fail-closed, never an unreserved request.
RESERVE_INPUT_TOKENS = 16384
# Sources that mean no provider request was sent: the reservation is released in full.
_UNSENT_SOURCES = frozenset({
    "context-rejected",
    "record-exists",
    "budget-rejected",
    "pricing-rejected",
    "replay-corrupt",
    "replay-miss",
})
# Sources that mean a request may have been sent even if the client's counter did not move.
_SENT_SOURCES = frozenset({"live-attempt", "live-error", "scripted-timeout"})
REASONS = (
    "disabled",
    "missing_journal",
    "inherited_env",
    "missing_credentials",
    "weak_code",
    "locked",
    "locked_all",
    "access_required",
    "ledger_unavailable",
    "ledger_unreconciled",
    "budget_exhausted",
    "ready",
)
# C3: Windows marks junctions, symbolic links and mount points as reparse points.
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
MANIFEST = "manifest.json"
OUTCOMES = "outcomes.jsonl"
# LiveSession(environ=PROCESS): read the process environment again at every check and keep
# no reference to it; a mapping is read the same way (tests); None keeps the config given.
PROCESS = "process"
# The access proof: HMAC-SHA256 keyed by the code (UTF-8) over this prefix and the nonce.
PROOF_CONTEXT = "corridor-live-access-v1|"
# Keys the grant fingerprint; random per process, never stored or shown.
_GRANT_SALT = secrets.token_bytes(32)


class LedgerUnavailable(BudgetExceeded):
    """The spend ledger or the journal could not be read or written: a fault, not a cap."""


class LedgerUnreconciled(BudgetExceeded):
    """C3: provider usage exceeded a reservation whose settlement failed; the ledger may be
    lower than the real cost, so nothing reserves until an operator reconciles it."""


def _redirected(path: Path) -> bool:
    """True when the path itself is a symbolic link, a junction or another reparse point."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(info.st_mode):
        return True
    return bool(getattr(info, "st_file_attributes", 0) & _REPARSE_POINT)


def private_directory(directory: Path) -> Path:
    """C3: create (or accept) a directory for the ledger or a journal, refusing redirection.

    The directory and every existing parent are checked before anything is created: none
    may be a symbolic link, a junction or another reparse point (``Path.is_symlink`` does
    not see an NTFS junction). On POSIX an existing directory must belong to this process's
    user and grant nothing to group or others; on Windows the ACL decides (documented).
    A check and a later open can still race with a local attacker who can write the
    parent; the directory is meant to be private to the service account.
    """
    path = Path(os.path.abspath(directory))
    for part in (path, *path.parents):
        if _redirected(part):
            raise OSError("Refusing a redirected path for the live ledger or journal")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if _redirected(path):
        raise OSError("Refusing a redirected path for the live ledger or journal")
    if sys.platform != "win32":
        info = os.stat(path)
        if info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise OSError("The live ledger or journal directory is not private")
    return path


def access_proof(code: str, nonce: str) -> str:
    """The proof the page's access field computes in the browser for one challenge."""
    return hmac.new(
        code.encode("utf-8"), (PROOF_CONTEXT + nonce).encode("ascii"), hashlib.sha256
    ).hexdigest()


def inherited_client_settings() -> list[str]:
    """Names (never values) of ``LLM_*`` variables the frozen client would inherit.

    ``LLMClient(env_file=None)`` reads these from the process environment (call and USD
    caps, reasoning effort, prices); the deployment table must stay the only source, so
    their presence makes live access unavailable instead of silently changing a request.
    """
    return sorted(name for name in os.environ if name in _ENV_FIELDS)


def _agent_rejected(entry: dict[str, Any]) -> bool:
    """The agent refused a recorded output after the client accepted it: a schema check
    marks the trace; a check that leaves no mark (a changed signed handle) is noted by
    the page through ``LiveSession.refuse_last``."""
    if entry["failed"]:
        return False
    trace = entry["trace"]
    return bool(
        entry.get("refused")
        or (trace and (trace.get("validation_errors") or trace.get("rejected_candidates")))
    )


def _step_of(entry: dict[str, Any]) -> str | None:
    trace = entry.get("trace")
    step = trace.get("step") if isinstance(trace, dict) else None
    return step if isinstance(step, str) else None


class _Ticket:
    """One call's reservation: its amount, how to settle it and the key for this call.

    Lives only for the duration of one ``JournalClient.complete``; the key is cleared as
    soon as the call returns, and nothing prints it.
    """

    __slots__ = ("amount", "key", "settle")

    def __init__(self, amount: Fraction, settle: Callable[[Fraction], None], key: str):
        self.amount, self.settle, self.key = amount, settle, key

    def __repr__(self) -> str:
        return f"_Ticket(amount={fraction_string(self.amount)})"


class SpendLedger:
    """SQLite serializes reservations and wrong access codes across sessions and processes.

    The spend cap is cumulative for the directory: no day, no calendar, no clock. Rows hold
    a currency, a deployment id and an amount, or the bare fact of one wrong access code:
    never a prompt, an output, a key or a code. A database error is a ``LedgerUnavailable``
    fault, distinct from a cap the reservation cannot fit (``BudgetExceeded``). The
    directory is created private to the service account where the platform supports it,
    and a symbolic link in place of the directory or the database is refused. The ledger
    is shared only by processes that see the same directory: several hosts need one
    shared directory, or the cap holds per host.

    With ``create=False`` (a public site, whose cap outlives the process: R8 finding S2)
    this object never starts a ledger: a missing file, or a file without the spend table,
    is a ``LedgerUnavailable`` fault, so a lost ledger closes live access instead of
    silently handing out a new budget. Only a separate setup step creates one.
    """

    FILE = "live-ledger.sqlite3"

    def __init__(self, directory: Path, *, create: bool = True):
        self.path = Path(directory) / self.FILE
        self._create = bool(create)

    def _connection(self) -> sqlite3.Connection:
        private_directory(self.path.parent)
        if _redirected(self.path):
            raise sqlite3.OperationalError("Refusing a redirected path for the live ledger")
        if self._create:
            connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        else:
            # ``mode=rw`` fails on a missing file where a plain connect would create one.
            connection = sqlite3.connect(
                self.path.resolve().as_uri() + "?mode=rw",
                uri=True,
                timeout=5,
                isolation_level=None,
            )
        try:
            connection.execute("BEGIN IMMEDIATE")
            if (
                not self._create
                and not connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'spend'"
                ).fetchone()
            ):
                # An emptied file is a lost ledger too, never a fresh one.
                raise sqlite3.OperationalError("The live ledger has no spend table")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS spend "
                "(id INTEGER PRIMARY KEY, currency TEXT NOT NULL, "
                "deployment TEXT NOT NULL, amount TEXT NOT NULL, settled INTEGER NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS access_failures (id INTEGER PRIMARY KEY)"
            )
            # C3: a reservation whose settlement failed while usage exceeded it; until an
            # operator removes these rows nothing reserves (the ledger may be too low).
            connection.execute(
                "CREATE TABLE IF NOT EXISTS unreconciled "
                "(id INTEGER PRIMARY KEY, deployment TEXT NOT NULL, amount TEXT NOT NULL)"
            )
            return connection
        except sqlite3.Error:
            connection.close()
            raise

    def initialize(self, currency: str, deployment_id: str, spent: Fraction) -> None:
        """Write the ledger's tables and any opening total, settled: the setup step of a
        ledger that nothing else may start, carrying a reconciled total over."""
        try:
            connection = self._connection()
            try:
                if spent:
                    connection.execute(
                        "INSERT INTO spend(currency, deployment, amount, settled) "
                        "VALUES (?, ?, ?, 1)",
                        (currency, deployment_id, fraction_string(spent)),
                    )
                connection.commit()
            finally:
                connection.close()
        except (OSError, sqlite3.Error, ValueError, ZeroDivisionError):
            raise LedgerUnavailable("Live spend ledger is unavailable") from None

    def unreconciled(self) -> int:
        try:
            connection = self._connection()
            try:
                return int(connection.execute("SELECT COUNT(*) FROM unreconciled").fetchone()[0])
            finally:
                connection.rollback()
                connection.close()
        except (OSError, sqlite3.Error, ValueError, TypeError):
            raise LedgerUnavailable("Live spend ledger is unavailable") from None

    def mark_unreconciled(self, deployment_id: str, amount: Fraction) -> None:
        try:
            connection = self._connection()
            try:
                connection.execute(
                    "INSERT INTO unreconciled(deployment, amount) VALUES (?, ?)",
                    (deployment_id, fraction_string(amount)),
                )
                connection.commit()
            finally:
                connection.close()
        except (OSError, sqlite3.Error, ValueError, ZeroDivisionError):
            raise LedgerUnavailable("Live spend ledger is unavailable") from None

    @staticmethod
    def _spent(connection: sqlite3.Connection, currency: str) -> Fraction:
        rows = connection.execute("SELECT amount FROM spend WHERE currency = ?", (currency,))
        return sum((Fraction(row[0]) for row in rows), Fraction(0))

    def remaining(self, currency: str, cap: Fraction) -> Fraction:
        try:
            connection = self._connection()
            try:
                return max(Fraction(0), cap - self._spent(connection, currency))
            finally:
                connection.rollback()
                connection.close()
        except (OSError, sqlite3.Error, ValueError, ZeroDivisionError):
            raise LedgerUnavailable("Live spend ledger is unavailable") from None

    def reserve(
        self, currency: str, deployment_id: str, amount: Fraction, cap: Fraction
    ) -> Callable[[Fraction], None]:
        if amount <= 0:
            raise BudgetExceeded("Invalid live request reservation")
        try:
            connection = self._connection()
            try:
                if connection.execute("SELECT COUNT(*) FROM unreconciled").fetchone()[0]:
                    connection.rollback()
                    raise LedgerUnreconciled("The live spend ledger awaits reconciliation")
                if self._spent(connection, currency) + amount > cap:
                    connection.rollback()
                    raise BudgetExceeded("Live spend cap cannot cover this request")
                cursor = connection.execute(
                    "INSERT INTO spend(currency, deployment, amount, settled) VALUES (?, ?, ?, 0)",
                    (currency, deployment_id, fraction_string(amount)),
                )
                reservation = cursor.lastrowid
                connection.commit()
            finally:
                connection.close()
        except BudgetExceeded:
            raise
        except (OSError, sqlite3.Error, ValueError, ZeroDivisionError):
            raise LedgerUnavailable("Live spend ledger is unavailable") from None

        def settle(actual: Fraction) -> None:
            try:
                connection = self._connection()
                try:
                    if actual < 0:
                        raise ValueError
                    connection.execute(
                        "UPDATE spend SET amount = ?, settled = 1 WHERE id = ? AND settled = 0",
                        (fraction_string(actual), reservation),
                    )
                    connection.commit()
                finally:
                    connection.close()
            except (OSError, sqlite3.Error, ValueError, ZeroDivisionError):
                # Failure keeps the earlier conservative reservation on the ledger.
                raise LedgerUnavailable("Live spend settlement is unavailable") from None

        return settle

    def access_failures(self) -> int:
        try:
            connection = self._connection()
            try:
                return int(connection.execute("SELECT COUNT(*) FROM access_failures").fetchone()[0])
            finally:
                connection.rollback()
                connection.close()
        except (OSError, sqlite3.Error, ValueError, TypeError):
            raise LedgerUnavailable("Live access ledger is unavailable") from None

    def record_access_failure(self) -> None:
        try:
            connection = self._connection()
            try:
                connection.execute("INSERT INTO access_failures DEFAULT VALUES")
                connection.commit()
            finally:
                connection.close()
        except (OSError, sqlite3.Error):
            raise LedgerUnavailable("Live access ledger is unavailable") from None


class JournalClient(LLMClient):
    """The frozen client in record mode, one call at a time, with the caps around it.

    Everything about the request, the transport, the output contract and the record file
    is ``LLMClient``'s own. This class only, for each call and under one lock: reserves on
    the ledger, sets the client's spend cap to that reservation (the frozen client then
    refuses before sending any request whose bound exceeds it), sets the key read for
    this call, takes exactly the one trace this call appended, clears the key and settles.
    """

    def __init__(self, session: "LiveSession", item: Deployment, **kwargs: Any) -> None:
        self._live_session, self.deployment = session, item
        self._serial = Lock()
        super().__init__(mode="record", **kwargs)
        # The key is not kept between calls: each call reads it again by path.
        self._api_key = ""

    def complete(self, **kwargs: Any) -> Any:  # type: ignore[override]
        clean: Exception | None = None
        with self._serial:
            ticket = self._live_session._reserve(self)
            calls_before, api_before = len(self.calls), self.api_calls
            self.max_usd = self.spent_usd + ticket.amount
            self._api_key = ticket.key
            try:
                result = super().complete(**kwargs)
            except Exception as error:
                # C3: the caller gets a fresh error of the same class with a checked message
                # and nothing else: no cause, no context (the provider's own exception may
                # echo the request's headers), no argument holding the key and no frame
                # below this call (those held the key). It is raised outside this block, so
                # it does not even reference the original as its context.
                clean = _clean_error(error, ticket.key)
                self._api_key = ticket.key = ""
                self._live_session._finish(
                    self, ticket, self.calls[calls_before:], api_before, failed=True
                )
            except BaseException:
                self._api_key = ticket.key = ""
                self._live_session._finish(
                    self, ticket, self.calls[calls_before:], api_before, failed=True
                )
                raise
            else:
                self._api_key = ticket.key = ""
                # C3: bookkeeping never replaces the call's own result (see _finish).
                self._live_session._finish(
                    self, ticket, self.calls[calls_before:], api_before, failed=False
                )
                return result
        assert clean is not None
        raise clean


def _clean_error(error: Exception, key: str) -> Exception:
    """C3: a fresh error of the same class carrying only a checked message."""
    message = str(error.args[0]) if error.args and isinstance(error.args[0], str) else ""
    if not message or (key and key in message) or len(message) > 400:
        message = f"{type(error).__name__} during a live model call"
    try:
        clean = type(error)(message)
    except Exception:
        clean = LLMConfigurationError(message)
    clean.__cause__ = None
    clean.__context__ = None
    clean.__suppress_context__ = True
    return clean


class LiveSession:
    def __init__(
        self,
        config: LiveConfig,
        *,
        environ: Mapping[str, str] | str | None = None,
        global_lock: bool = True,
        ledger_create: bool = True,
    ) -> None:
        self.config = config
        self._environ = environ
        # Whether wrong codes across every session of the ledger block new grants
        # (``GLOBAL_LOCK_AFTER``); the default. Off, only the per-session lock remains.
        self._global_lock = bool(global_lock)
        self._lock = RLock()
        # Whether this session may start a ledger that is not there; the default. Off, a
        # missing ledger is a fault (see ``SpendLedger``).
        self._ledger_create = bool(ledger_create)
        self._ledger = self._open_ledger(config)
        self._authorized = False
        # A keyed fingerprint of the code source and the credential files at the grant.
        self._grant: str | None = None
        # Why access ended while a live client existed: disabled, credentials, locked.
        self._revoked: str | None = None
        self._nonce: str | None = None
        self._failures = 0
        self._deployment_id = DEPLOYMENTS[0].id
        self._client: JournalClient | None = None
        self._owner: object = None
        self._calls = 0
        self._in_flight = 0
        # Provider requests of clients this session already let go, and every journal
        # directory it used: a return to the scripted planner never hides either.
        self._api_released = 0
        self._journals: list[Path] = []
        self._spent: dict[str, Fraction] = {currency: Fraction(0) for currency in CURRENCIES}
        self._outcomes: list[dict[str, Any]] = []
        self._ledger_fault = False
        self._overrun = False
        self._select_default()

    # --- configuration ---------------------------------------------------------------

    def _open_ledger(self, config: LiveConfig) -> SpendLedger | None:
        if config.journal_dir is None:
            return None
        return SpendLedger(config.journal_dir, create=self._ledger_create)

    def _values(self) -> Mapping[str, str] | None:
        """The environment to read now, or None for a session bound to its config only."""
        if self._environ == PROCESS:
            return os.environ
        if isinstance(self._environ, Mapping):
            return self._environ
        return None

    def _current_config(self) -> LiveConfig | None:
        """The configuration in force right now; None when it no longer parses."""
        values = self._values()
        if values is None:
            return self.config
        try:
            return LiveConfig.from_environ(values)
        except LLMConfigurationError:
            return None

    def refresh(self, config: LiveConfig) -> None:
        with self._lock:
            if config == self.config:
                return
            if not config.enabled:
                self._revoke("disabled")
            elif config.env_dir != self.config.env_dir:
                self._revoke("credentials")
            if config.journal_dir != self.config.journal_dir:
                self._ledger = self._open_ledger(config)
                self._ledger_fault = self._overrun = False
            self.config = config
            self._drop_client()

    def _revoke(self, reason: str) -> None:
        """End the grant now; the live client of this session refuses its next call."""
        if self._authorized or self._client is not None:
            self._revoked = reason
        self._authorized, self._grant = False, None

    def _select_default(self) -> None:
        for item in DEPLOYMENTS:
            if self.credential_status(item) == "ok":
                self._deployment_id = item.id
                return

    def select(self, deployment_id: str) -> None:
        item = deployment(deployment_id)
        with self._lock:
            if item.id != self._deployment_id:
                self._deployment_id = item.id
                self._drop_client()

    @property
    def deployment(self) -> Deployment:
        return deployment(self._deployment_id)

    def _credential(self, item: Deployment) -> Credential | None:
        path = self.config.credential_path(item)
        return read_credential(path) if path is not None else None

    def credential_status(self, item: Deployment) -> str:
        """``ok`` or why this deployment cannot be used; nothing about the key itself."""
        path = self.config.credential_path(item)
        if path is None:
            return "missing_dir"
        if not path.is_file():
            return "missing_file"
        credential = read_credential(path)
        if credential is None:
            return "no_key"
        if (credential.api_base and credential.api_base != item.api_base) or (
            credential.api and credential.api.replace("chat.completions", "chat") != item.api
        ):
            # The key is only ever sent to the registry's fixed endpoint for its provider.
            return "endpoint_mismatch"
        return "ok"

    def _expected_code(self) -> str:
        """The code in force now (the environment's, else the first credential file's).

        Read at the moment of a check and returned to that check only; never stored.
        """
        values = self._values()
        if values is not None:
            code = environ_access_code(values)
            if code:
                return code
        for item in DEPLOYMENTS:
            credential = self._credential(item)
            if credential is not None and credential.access_code:
                return credential.access_code
        return ""

    def _read_files(self) -> dict[str, bytes]:
        """C3: one reading of every credential file (missing / unreadable marked as such);
        a call's grant check and its key come from the same reading."""
        files = {}
        for name in sorted({item.env_file for item in DEPLOYMENTS}):
            path = self.config.env_dir / name if self.config.env_dir is not None else None
            try:
                files[name] = (
                    path.read_bytes() if path is not None and path.is_file() else b"\0missing"
                )
            except OSError:
                files[name] = b"\0unreadable"
        return files

    def _fingerprint(self, files: dict[str, bytes] | None = None) -> str:
        """Keyed by a per-process random salt: the code source and every credential file.

        Any rotated code, key or endpoint gives a different fingerprint; the value reveals
        nothing about them without the salt, which is never stored or shown.
        """
        mac = hmac.new(_GRANT_SALT, digestmod=hashlib.sha256)
        values = self._values()
        code = environ_access_code(values) if values is not None else ""
        mac.update(b"env\0" + hashlib.sha256(code.encode("utf-8")).digest())
        for name, data in sorted((files if files is not None else self._read_files()).items()):
            mac.update(name.encode("utf-8") + b"\0" + hashlib.sha256(data).digest())
        return mac.hexdigest()

    def _unavailable(self, item: Deployment) -> str | None:
        """Why no session could use live access now, before any access code is checked."""
        if not self.config.enabled:
            return "disabled"
        if self._ledger is None:
            return "missing_journal"
        if inherited_client_settings():
            return "inherited_env"
        if self.credential_status(item) != "ok":
            return "missing_credentials"
        code = self._expected_code()
        if code and not access_code_strong(code):
            # C3: a short or repetitive code could be recovered offline from the proof the
            # page keeps; the operator must configure a long random one.
            return "weak_code"
        return None

    def ledger_ready(self) -> bool:
        """Whether the ledger answers right now; a public site's gate reads this."""
        if self._ledger is None:
            return False
        try:
            self._ledger.unreconciled()
        except BudgetExceeded:
            self._ledger_fault = True
            return False
        return True

    def _unreconciled(self) -> bool:
        if self._ledger is None:
            return False
        try:
            return self._ledger.unreconciled() > 0
        except BudgetExceeded:
            self._ledger_fault = True
            return False

    # --- access ----------------------------------------------------------------------

    def _global_failures(self) -> int:
        if self._ledger is None:
            return 0
        try:
            return self._ledger.access_failures()
        except BudgetExceeded:
            self._ledger_fault = True
            return 0

    def challenge(self) -> str:
        """The single-use nonce the page's access field hashes the code with."""
        with self._lock:
            if self._nonce is None:
                self._nonce = secrets.token_hex(16)
            return self._nonce

    def authorize(self, code: str) -> str:
        """Check a code by computing its proof here (tests and tools; the page never
        holds a code: its access field sends only the proof)."""
        nonce = self.challenge()
        proof = access_proof(code, nonce) if isinstance(code, str) else ""
        return self.authorize_proof(nonce, proof)

    def authorize_proof(self, nonce: Any, proof: Any) -> str:
        """Check the proof for the current challenge; the challenge is used up either way."""
        with self._lock:
            current, self._nonce = self._nonce, None
            if (
                current is None
                or not isinstance(nonce, str)
                or not nonce.isascii()
                or not hmac.compare_digest(nonce, current)
            ):
                return "stale"
            if self._unavailable(self.deployment) is not None or self._ledger is None:
                return "unavailable"
            if self._failures >= LOCK_AFTER_FAILURES:
                return "locked"
            if self._global_lock:
                try:
                    if self._ledger.access_failures() >= GLOBAL_LOCK_AFTER:
                        return "locked_all"
                except BudgetExceeded:
                    self._ledger_fault = True
                    return "unavailable"
            expected = self._expected_code()
            if not expected:
                return "unavailable"
            accepted = (
                isinstance(proof, str)
                and proof.isascii()
                and hmac.compare_digest(proof, access_proof(expected, current))
            )
            del expected
            if accepted:
                self._authorized, self._grant = True, self._fingerprint()
                self._failures, self._revoked = 0, None
                return "accepted"
            self._failures += 1
            if self._failures >= LOCK_AFTER_FAILURES:
                # A locked session has no live access, whatever it was granted before.
                self._revoke("locked")
            try:
                self._ledger.record_access_failure()
            except BudgetExceeded:
                # Without a ledger nothing live can be reserved either; stay closed.
                self._ledger_fault = True
            return "invalid"

    def _grant_current(self, files: dict[str, bytes] | None = None) -> bool:
        """The grant still matches the code source and the credential files; else revoked."""
        if not self._authorized:
            return False
        if self._grant is None or not hmac.compare_digest(self._grant, self._fingerprint(files)):
            self._revoke("credentials")
            return False
        return True

    # --- state -----------------------------------------------------------------------

    def _remaining(self, currency: str) -> Fraction:
        if not self.config.enabled or self._ledger is None:
            return Fraction(0)
        try:
            remaining = self._ledger.remaining(currency, self.config.budget_cap(currency))
        except BudgetExceeded:
            self._ledger_fault = True
            return Fraction(0)
        # The ledger answers again: a fault is checked afresh by the next reservation.
        self._ledger_fault = False
        return remaining

    @staticmethod
    def _reservation(item: Deployment) -> Fraction:
        """The assumed cost one call reserves before it is sent."""
        return (
            Fraction(RESERVE_INPUT_TOKENS) * Fraction(item.price_in)
            + Fraction(item.max_output_tokens) * Fraction(item.price_out)
        ) / 1_000_000

    def _reason(self, item: Deployment, remaining: Fraction, global_failures: int) -> str:
        unavailable = self._unavailable(item)
        if unavailable is not None:
            return unavailable
        if self._failures >= LOCK_AFTER_FAILURES:
            return "locked"
        if self._global_lock and global_failures >= GLOBAL_LOCK_AFTER and not self._authorized:
            # C3: other sessions' wrong codes block new grants, never an existing one.
            return "locked_all"
        if not self._grant_current() or not self._expected_code():
            return "access_required"
        if self._unreconciled():
            return "ledger_unreconciled"
        if self._ledger_fault:
            return "ledger_unavailable"
        if (
            self._calls >= self.config.session_calls
            or remaining < self._reservation(item)
            or self._overrun
        ):
            return "budget_exhausted"
        return "ready"

    def _display_path(self, path: Path) -> str:
        """A journal directory relative to ``LIVE_JOURNAL_DIR``: no host path, no user name."""
        root = self.config.journal_dir
        try:
            return path.relative_to(root).as_posix() if root is not None else path.name
        except ValueError:
            return path.name

    def snapshot(self) -> dict[str, Any]:
        """Everything the page may show; never a key, a code or a path to a key."""
        with self._lock:
            item = self.deployment
            remaining = {currency: self._remaining(currency) for currency in CURRENCIES}
            global_failures = (
                self._global_failures() if self.config.enabled and self._global_lock else 0
            )
            reason = self._reason(item, remaining[item.currency], global_failures)
            return {
                "enabled": self.config.enabled,
                "credentials_dir": self.config.env_dir is not None,
                "journal_configured": self._ledger is not None,
                "inherited_env": inherited_client_settings() if self.config.enabled else [],
                "deployment": item.id,
                "deployment_label": item.label,
                "deployments": [
                    {
                        "id": candidate.id,
                        "label": candidate.label,
                        "model": candidate.model,
                        "provider": candidate.provider,
                        "currency": candidate.currency,
                        "price_in": candidate.price_in,
                        "price_out": candidate.price_out,
                        "price_note": candidate.price_note,
                        "credential": self.credential_status(candidate)
                        if self.config.enabled
                        else "disabled",
                    }
                    for candidate in DEPLOYMENTS
                ],
                "authorized": self._authorized,
                "revoked": self._revoked,
                "locked": self._failures >= LOCK_AFTER_FAILURES,
                "locked_all": self._global_lock
                and global_failures >= GLOBAL_LOCK_AFTER
                and not self._authorized,
                "ledger_fault": self._ledger_fault,
                "available": reason == "ready",
                "reason": reason,
                "calls": self._calls,
                "api_calls": self._api_released
                + (self._client.api_calls if self._client is not None else 0),
                "session_calls": self.config.session_calls,
                "calls_remaining": max(0, self.config.session_calls - self._calls),
                "spent": {
                    currency: fraction_string(self._spent[currency]) for currency in CURRENCIES
                },
                "remaining": {
                    currency: fraction_string(remaining[currency]) for currency in CURRENCIES
                },
                "reservation": fraction_string(self._reservation(item)),
                "journal_dir": self._display_path(self._client.replay_dir)
                if self._client is not None
                else None,
                "journal_dirs": [self._display_path(path) for path in self._journals],
                "last_call": self.last_call(),
            }

    @property
    def available(self) -> bool:
        return bool(self.snapshot()["available"])

    @property
    def in_flight(self) -> bool:
        """A live call is running right now (it holds the transaction boundary too)."""
        with self._lock:
            return self._in_flight > 0

    def is_current(self, client: object) -> bool:
        """The client is this session's live client and may still call (not revoked)."""
        with self._lock:
            return client is not None and client is self._client and self._authorized

    # --- outcomes: one entry per call attempt, read per operation ----------------------

    def _record(
        self,
        client: JournalClient,
        trace: dict[str, Any] | None,
        source: str,
        *,
        failed: bool,
        settlement: str = "released",
        overrun: bool = False,
        unreconciled: bool = False,
    ) -> None:
        self._outcomes.append({
            "client": client,
            "trace": trace,
            "source": source,
            "failed": failed,
            "settlement": settlement,
            "overrun": overrun,
            "unreconciled": unreconciled,
            "flushed": False,
        })

    def mark(self) -> int:
        """Where the next operation's outcomes start."""
        with self._lock:
            return len(self._outcomes)

    def outcomes(self, since: int = 0) -> list[dict[str, Any]]:
        """Each attempt since ``since``: its source, whether it failed, whether the agent
        refused the recorded output afterwards (a schema or handle check), and how its
        cost was settled (``settled`` usage, a pre-send ``bound``, ``released`` unsent,
        ``kept`` reservation, or ``failed`` settlement)."""
        with self._lock:
            return [
                {
                    "source": entry["source"],
                    "failed": entry["failed"],
                    "rejected": _agent_rejected(entry),
                    "settlement": entry["settlement"],
                    "overrun": entry["overrun"],
                    "unreconciled": entry.get("unreconciled", False),
                    "deployment": entry["client"].deployment.label,
                    # The call's step name, so that a page can tell a suggestion's call
                    # (agent/domestic_suggest.py) from the reply's own.
                    "step": _step_of(entry),
                }
                for entry in self._outcomes[max(0, since) :]
            ]

    def refuse_last(self, since: int) -> None:
        """The operation since ``since`` failed on the model's output itself (the agent
        raised an output error) while no attempt failed at the client: the last recorded
        output is the refused one. Called before the attempt is indexed."""
        with self._lock:
            entries = self._outcomes[max(0, since) :]
            if entries and not any(entry["failed"] for entry in entries):
                entries[-1]["refused"] = True

    def last_call(self) -> dict[str, Any] | None:
        with self._lock:
            if not self._outcomes:
                return None
            entry = self._outcomes[-1]
            return {"source": entry["source"], "failed": entry["failed"]}

    def _flush(self) -> None:
        """Append every attempt not yet indexed to its journal's ``outcomes.jsonl``.

        An attempt is indexed when the next attempt starts or when the page finishes the
        operation, by which time the agent has accepted or refused its output.
        """
        pending = [entry for entry in self._outcomes if not entry["flushed"]]
        lines: dict[Path, list[str]] = {}
        for entry in pending:
            trace = entry["trace"] or {}
            step = trace.get("step")
            record = {
                "request_hash": trace.get("request_hash"),
                "step": step if isinstance(step, str) else None,
                "source": entry["source"],
                "failed": entry["failed"],
                "agent_rejected": _agent_rejected(entry),
                "settlement": entry["settlement"],
            }
            lines.setdefault(entry["client"].replay_dir, []).append(
                json.dumps(record, sort_keys=True, ensure_ascii=True)
            )
        for directory, rows in lines.items():
            with (directory / OUTCOMES).open("a", encoding="utf-8") as stream:
                stream.write("".join(row + "\n" for row in rows))
        for entry in pending:
            entry["flushed"] = True

    def flush(self) -> bool:
        """Index finished attempts now; False when the journal could not be written (the
        next call then refuses to start)."""
        with self._lock:
            try:
                self._flush()
            except OSError:
                return False
            return True

    # --- the client ------------------------------------------------------------------

    def _journal_dir(self, item: Deployment) -> Path:
        assert self.config.journal_dir is not None
        # C3: no symbolic link, junction or other reparse point on the way (Windows junctions
        # are not seen by Path.is_symlink), and on POSIX a directory private to this user.
        base = self.config.journal_dir / item.id
        private_directory(base)
        index = 1
        while True:
            candidate = base / f"session-{index:04d}"
            try:
                os.mkdir(candidate, 0o700)
            except FileExistsError:
                index += 1
                continue
            return candidate

    @staticmethod
    def _write_manifest(directory: Path, item: Deployment) -> None:
        manifest = {
            "schema": "corridor-live-journal-v1",
            "deployment": item.id,
            "provider": item.provider,
            "model": item.model,
            "api": item.api,
            "api_base": item.api_base,
            "currency": item.currency,
            "assumed_price_in": item.price_in,
            "assumed_price_out": item.price_out,
            "price_unit": f"{item.currency} per million tokens (assumed prices, not an invoice)",
            "cost_field_note": (
                f"The cost_usd field of every record in this directory is in {item.currency}, "
                "the deployment's currency, computed from the assumed prices above; the "
                "client's max_usd and spent_usd use the same unit."
            ),
            "reservation_note": (
                f"Each call reserved {RESERVE_INPUT_TOKENS} input tokens plus the output cap "
                "at the assumed prices; the client refused before sending any request whose "
                "own bound (request bytes plus 1024 framing tokens, plus the output cap) "
                "exceeded that reservation."
            ),
            "outcomes_index": OUTCOMES,
            "outcomes_note": (
                "One line per call attempt: source, failed, agent_rejected when the agent "
                "refused an output after it was recorded, and settlement: settled (provider "
                "usage), bound (sent, usage unknown: the client's pre-send bound), released "
                "(not sent), kept (the reservation stays) or failed (the ledger could not "
                "be updated; the reservation stays). A record file alone does not mean the "
                "output was used."
            ),
        }
        (directory / MANIFEST).write_text(
            json.dumps(manifest, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def _drop_client(self) -> None:
        """Forget the current client, keeping its provider requests in the session count."""
        if self._client is not None:
            try:
                self._flush()
            except OSError:
                pass
            self._api_released += self._client.api_calls
        self._client, self._owner = None, None

    def client(self, owner: object, *, context_guard: Callable[[object], None] | None) -> Any:
        """The record-mode client for this session, bound to one controller."""
        with self._lock:
            if not self.available:
                raise BudgetExceeded("Live mode requires current access and available budget")
            if self._client is not None and self._owner is owner:
                if context_guard is not None:
                    self._client.context_guard = context_guard
                return self._client
            item = self.deployment
            credential = self._credential(item)
            if credential is None:
                raise LLMConfigurationError("Live credential file is unavailable")
            journal = self._journal_dir(item)
            failure: Exception | None = None
            try:
                client = JournalClient(
                    self,
                    item,
                    replay_dir=journal,
                    model=item.model,
                    api=item.api,
                    api_base=item.api_base,
                    provider=item.provider,
                    api_key=credential.api_key,
                    env_file=None,
                    max_calls=max(1, self.config.session_calls),
                    # Lowered to each call's own reservation before the call (see above).
                    max_usd=self._reservation(item),
                    max_output_tokens=item.max_output_tokens,
                    reasoning_effort=item.reasoning_effort,
                    assumed_price_in=item.price_in,
                    assumed_price_out=item.price_out,
                    context_guard=context_guard,
                )
                self._write_manifest(journal, item)
            except (LLMConfigurationError, OSError) as error:
                for path in journal.iterdir():
                    path.unlink()
                journal.rmdir()
                # C3: a fresh error (the original's frames held the key it was built with).
                failure = _clean_error(error, credential.api_key)
            finally:
                del credential
            if failure is not None:
                raise failure
            self._drop_client()
            self._client, self._owner = client, owner
            self._revoked = None
            self._journals.append(journal)
            return client

    def release(self) -> None:
        """Forget the client (the page returned to the scripted planner)."""
        with self._lock:
            self._drop_client()
            self._revoked = None

    def _refusal(
        self, client: JournalClient, files: dict[str, bytes]
    ) -> tuple[tuple[str, str] | None, LiveConfig | None]:
        """Why this client may not send a request now: ((source, message) or None, and the
        configuration read for this call).

        Checked before every call, not only at the transaction boundary: a switch turned
        off, a configuration that no longer parses, a rotated code or key, a lock or a
        revoked grant stop new external calls at once. C3: other sessions' wrong codes do
        not end this grant (they block new grants only), and the grant is checked against
        the one reading of the credential files this call's key comes from.
        """
        if client is not self._client:
            return ("live-revoked", "This live client is no longer current"), None
        current = self._current_config()
        if current is None or not current.enabled:
            self._revoke("disabled")
            return ("live-revoked", "Live mode is switched off"), None
        if (current.env_dir, current.journal_dir) != (
            self.config.env_dir,
            self.config.journal_dir,
        ):
            self._revoke("credentials")
            return ("live-revoked", "Live configuration changed"), None
        if self._ledger is None or inherited_client_settings():
            return ("live-revoked", "Live access is no longer available in this session"), None
        if self._failures >= LOCK_AFTER_FAILURES:
            self._revoke("locked")
            return ("live-revoked", "Live access is locked in this session"), None
        if not self._grant_current(files):
            return ("live-revoked", "Live access was revoked"), None
        if self.credential_status(client.deployment) != "ok":
            self._revoke("credentials")
            return ("live-revoked", "Live credential file is unavailable"), None
        return None, current

    def _reserve(self, client: JournalClient) -> _Ticket:
        with self._lock:
            item = client.deployment
            try:
                self._flush()
            except OSError:
                self._record(client, None, "ledger-unavailable", failed=True)
                raise LedgerUnavailable("Live journal index is unavailable") from None
            files = self._read_files()
            refusal, current = self._refusal(client, files)
            if refusal is not None:
                self._record(client, None, refusal[0], failed=True)
                if refusal[0] == "ledger-unavailable":
                    raise LedgerUnavailable(refusal[1])
                raise BudgetExceeded(refusal[1])
            # C3: the caps read for this call apply to it (a lowered cap applies at once).
            config = current or self.config
            if self._calls >= config.session_calls:
                self._record(client, None, "budget-rejected", failed=True)
                raise BudgetExceeded("Session live call cap reached")
            if self._overrun:
                self._record(client, None, "budget-rejected", failed=True)
                raise BudgetExceeded("Provider usage exceeded an earlier reservation")
            amount = self._reservation(item)
            assert self._ledger is not None
            try:
                settle = self._ledger.reserve(
                    item.currency, item.id, amount, config.budget_cap(item.currency)
                )
            except LedgerUnreconciled:
                self._record(client, None, "ledger-unreconciled", failed=True)
                raise
            except LedgerUnavailable:
                self._ledger_fault = True
                self._record(client, None, "ledger-unavailable", failed=True)
                raise
            except BudgetExceeded:
                self._record(client, None, "budget-rejected", failed=True)
                raise
            credential = parse_credential(files.get(item.env_file, b""))
            if credential is None:
                try:
                    settle(Fraction(0))
                except BudgetExceeded:
                    self._ledger_fault = True
                self._revoke("credentials")
                self._record(client, None, "live-revoked", failed=True)
                raise BudgetExceeded("Live credential file is unavailable")
            self._calls += 1
            self._spent[item.currency] += amount
            self._in_flight += 1
            ticket = _Ticket(amount, settle, credential.api_key)
            del credential
            return ticket

    def _finish(
        self,
        client: JournalClient,
        ticket: _Ticket,
        traces: list[dict[str, Any]],
        api_before: int,
        *,
        failed: bool,
    ) -> None:
        """Settle one call and record its outcome; never raises over the call's own result.

        C3: the bookkeeping runs in a guarded block. Whatever it raises (a settlement the
        ledger driver refuses in an unexpected way, a malformed trace, a record that cannot
        be kept) becomes a fault record (``settlement: failed``, the reservation stays on
        the ledger, the ledger is checked again before the next call), and the call is
        released from ``in_flight`` exactly once, in ``finally``.
        """
        try:
            self._account(client, ticket, traces, api_before, failed=failed)
        except Exception:
            with self._lock:
                self._ledger_fault = True
                try:
                    self._record(
                        client, None, "ledger-unavailable", failed=failed, settlement="failed"
                    )
                except Exception:  # nothing further may replace the call's own outcome
                    pass
        finally:
            with self._lock:
                self._in_flight = max(0, self._in_flight - 1)

    def _account(
        self,
        client: JournalClient,
        ticket: _Ticket,
        traces: list[dict[str, Any]],
        api_before: int,
        *,
        failed: bool,
    ) -> None:
        """Exactly one trace belongs to one ``complete`` (the call is serialized). Not sent:
        released. Sent with validated provider usage: settled at that cost. Sent with no
        usage (a transport error, a timeout): settled at the client's own pre-send bound,
        never at zero. Anything unclear keeps the whole reservation. A settlement the
        ledger refuses is recorded as ``failed`` and keeps the reservation. Usage above the
        reservation stops live calls in this session; if its settlement also failed, the
        ledger may be lower than the real cost and is marked "to be reconciled" for every
        session (best effort: a ledger that cannot be written cannot record that either).
        """
        trace = traces[0] if len(traces) == 1 else None
        source = str(trace.get("source", "unknown")) if trace is not None else "unknown"
        sent = client.api_calls > api_before or source in _SENT_SOURCES
        cost = trace.get("cost_usd") if trace is not None else None
        validated = trace is not None and "input_tokens" in trace and "output_tokens" in trace
        amount: Fraction | None
        if not sent and source in _UNSENT_SOURCES:
            amount, settlement = Fraction(0), "released"
        elif sent and isinstance(cost, Fraction) and cost > 0:
            amount, settlement = cost, "settled" if validated else "bound"
        else:
            amount, settlement = None, "kept"
        if amount is not None:
            try:
                ticket.settle(amount)
            except Exception:
                settlement = "failed"
        overrun = amount is not None and amount > ticket.amount
        unreconciled = overrun and settlement == "failed"
        if unreconciled and self._ledger is not None:
            try:
                self._ledger.mark_unreconciled(client.deployment.id, amount)
            except BudgetExceeded:
                pass
        with self._lock:
            if settlement == "failed":
                self._ledger_fault = True
            elif amount is not None:
                self._spent[client.deployment.currency] += amount - ticket.amount
            if overrun:
                # The provider reported more than the bound: stop live calls in this
                # session; the page says whether the ledger holds the real cost.
                self._overrun = True
            self._record(
                client,
                trace,
                source,
                failed=failed,
                settlement=settlement,
                overrun=overrun,
                unreconciled=unreconciled,
            )
