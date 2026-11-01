"""Request handlers: check the transport, find the session, run the work, answer JSON.

Every session request's executor job runs under the session's lock and checks the
request's generation against the session's first; the routing, the transport check, the
body parser, the session lookup and the pool choice (``live.may_call``) come before the
job, outside the lock. A business POST is ``live.sync → service → live.sync → refresh →
view model``; shell POSTs skip the live sync; GETs only read view models and never
refresh, issue tokens or touch a controller's state. While the ``lab`` capability is
closed (the submission build), every ``/api/lab`` path answers 404 first.

A Write action's product calls are counted by the session's proxies (``state.Product``):
once the action has called a product method outside the read-only allow list, a refusal
or a contained result it raises is answered as an unknown result and the session is
distrusted, whatever route or service function raised it (RW-5 item 2). Only a refusal
raised before any such call keeps its status.
"""

import contextlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tornado.web

import corridor.web
from corridor.i18n import reset_locale, set_locale
from corridor.ui.audience import reset_audience, set_audience
from corridor.web import lab, logs, rogue, security, suggest
from corridor.web import public as public_mode
from corridor.web import state as web_state
from corridor.web.errors import WebError, WebOperationFailed, WebOutcomeUnknown, body, error
from corridor.web.executor import bound
from corridor.web.live import expected_failure
from corridor.web.security import (
    CARD_VERSION,
    CHOICE,
    CLIENT_MSG_ID,
    CODE,
    DEPLOYMENT,
    GATE_VERSION,
    GENERATION,
    GESTURE,
    LOCALES,
    NODE,
    NONCE,
    PAIR,
    PERIOD,
    PROOF,
    RKEY,
    SHORTCUTS,
    TAB,
    TKEY,
    VIEWS,
    Check,
    anything,
    boolean,
    distinct,
    flag,
    matching,
    nullable,
    obj,
    one_of,
    string,
)
from corridor.web.sessions import default_locale

STATIC = Path(__file__).with_name("static")
HMAC_JS = Path(__file__).resolve().parents[1] / "ui" / "live_access" / "hmac.js"
MAX_CHARS = 4000
MAX_AA = 20
TAB_NAMES = ("accounts", "wealth", "plans", "operator")
UNLOCK_RESULTS = ("accepted", "invalid", "locked", "locked_all", "stale", "unavailable")
# The statuses a login or a device gesture may answer with (contract StatusVM).
STATUSES = frozenset({
    "accepted",
    "logged_in",
    "complete",
    "ready",
    "login_required",
    "settled",
    "controlled",
    "awaiting_approval",
    "waiting_mfa",
    "mfa_verified",
    "redisplay",
    "selection_required",
    "intent_confirmation_required",
    "rejected",
    "refused",
    "failed",
    "blocked",
    "withdrawn",
    "interrupted",
    "pending",
    "running",
    "waiting_settlement",
    "succeeded",
    "completed",
    "draft",
})
PARAMS = {"rkey": RKEY, "tkey": TKEY, "pair": PAIR}
# Static files are served by extension only; pages come from their own routes.
CONTENT_TYPES = {
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".woff2": "font/woff2",
    ".ico": "image/x-icon",
}
# Statuses tornado raises by itself, mapped onto the contract's codes.
HTTP_CODES = {400: "bad_json", 404: "not_found", 405: "method_not_allowed", 413: "too_large"}
# Every path of the authorization lab, its routes and any other path below them.
LAB_PATHS = "/api/lab"


class Moved(Exception):
    """The session was reset or evicted while this request waited for its lock."""


def _failed(handler: Any, kind: type[BaseException]) -> None:
    # A failure is logged by its class name only. A log call that fails is ignored, so it
    # never replaces the failure being handled or skips the distrust (RW-4'''').
    with contextlib.suppress(Exception):
        logs.failure(handler, kind)


def features(settings: Mapping[str, Any]) -> frozenset[str]:
    configured = settings.get("features")
    # Read on each request, so the view models and the routes see the same set.
    return frozenset(configured) if configured is not None else corridor.web.FEATURES


def public(settings: Mapping[str, Any]) -> Any:
    """The application's ``PublicConfig``, or None outside public mode (design §3.2)."""
    return settings.get("public")


def ok(ws: Any, extras: Mapping[str, Any] | None = None, state: Any = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"ok": True, "generation": ws.generation, "revision": ws.revision}
    payload.update(extras or {})
    if state is not None:
        payload["state"] = state
    _same_version(ws, payload.get("state"))
    return payload


def _same_version(ws: Any, state: Any) -> None:
    # A response's generation and revision are always those of the state it carries.
    if state is not None and (
        state.get("generation") != ws.generation or state.get("revision") != ws.revision
    ):
        raise error("internal")


class Base(tornado.web.RequestHandler):
    route = logs.UNMATCHED
    kind = "page"
    methods: tuple[str, ...] = ("GET",)

    def initialize(self, route: str | None = None, **options: Any) -> None:
        if route is not None:
            self.route = route
        self.options = options
        self.tab: str | None = None
        self.generation: str | None = None
        self.ws: Any = None
        # Public mode: the client address the limits key on, and the browser cookie.
        self.source: str | None = None
        self.browser: str | None = None

    def set_default_headers(self) -> None:
        self.clear_header("Server")
        for name, value in security.RESPONSE_HEADERS.items():
            self.set_header(name, value)
        if self.request.path.startswith(("/api/", "/phone/")):
            self.set_header("Cache-Control", "no-store")

    def compute_etag(self) -> str | None:
        return None

    def prepare(self) -> None:
        if self.request.path.startswith(LAB_PATHS) and lab.FEATURE not in features(self.settings):
            # The submission build closes the authorization lab (RW-4', RW-4'', RW-4'''):
            # any request to it answers 404 here, before its method, transport headers,
            # body, session or generation is looked at, and no session is touched.
            raise error("not_found")
        if self.request.method not in self.methods:
            raise error("method_not_allowed")
        config = public(self.settings)
        self.tab, self.generation = security.check(
            self.request.headers, self.settings["port"], kind=self.kind, public=config
        )
        if config is not None:
            self.throttle()

    def throttle(self) -> None:
        """Public mode: the request's rate buckets, keyed by the client address, after
        the transport check and before its body, session or lock (design §4.3). Over a
        limit the answer is 503 ``busy`` with ``Retry-After`` and nothing else is touched.
        """
        limiter = self.settings["limits"].limiter
        self.source = public_mode.client_ip(self.request.headers)
        for scope in public_mode.scopes(self.request.path):
            if not limiter.allow(scope, self.source):
                raise error("busy")

    def respond(self, status: int, data: bytes) -> None:
        self.set_status(status)
        self.set_header("Content-Type", "application/json; charset=utf-8")
        self.finish(data)

    def write_error(self, status_code: int, **kwargs: Any) -> None:
        exc = kwargs.get("exc_info", (None, None, None))[1]
        if not isinstance(exc, WebError):
            exc = error(HTTP_CODES.get(status_code, "internal"))
        if exc.code == "busy" and public(self.settings) is not None:
            # Every public-mode refusal for capacity (a bucket, a place in flight, the
            # session table) says when to retry; set here, since tornado clears the headers
            # before an error is written.
            self.set_header("Retry-After", public_mode.RETRY_AFTER)
        self.respond(exc.status, security.dumps(self._error_body(exc)))

    def _error_body(self, exc: WebError) -> dict[str, Any]:
        ws = self.ws
        locale = set_locale(ws.locale if ws is not None else default_locale(None))
        audience = set_audience(ws.view if ws is not None else "customer")
        try:
            return body(exc)
        finally:
            reset_audience(audience)
            reset_locale(locale)

    def log_exception(self, typ: Any, value: Any, tb: Any) -> None:
        if isinstance(value, (WebError, tornado.web.HTTPError)):
            return
        logs.failure(self, typ)


class Missing(Base):
    methods = ("GET", "HEAD", "POST", "PUT", "DELETE", "PATCH")

    def prepare(self) -> None:
        super().prepare()
        raise error("not_found")


class Page(Base):
    """``index.html`` and the simulated phone page, read from disk on each request."""

    def get(self, pair: str | None = None) -> None:
        if pair is not None and not PAIR.fullmatch(pair):
            raise error("not_found")
        try:
            content = (STATIC / self.options["file"]).read_bytes()
        except FileNotFoundError:
            raise error("not_found") from None
        self.set_header("Content-Type", "text/html; charset=utf-8")
        self.set_header("Cache-Control", "no-store")
        self.finish(content)


class Vendor(Base):
    """The browser-side HMAC of the live access field, only while the live switch is open."""

    def get(self) -> None:
        if "live" not in features(self.settings):
            raise error("not_found")
        self.set_header("Content-Type", CONTENT_TYPES[".js"])
        if public(self.settings) is not None:
            self.set_header("Cache-Control", "no-cache")
        self.finish(HMAC_JS.read_bytes())


class Static(Base, tornado.web.StaticFileHandler):
    methods = ("GET", "HEAD")
    compute_etag = tornado.web.StaticFileHandler.compute_etag

    def initialize(self, route: str | None = None, **options: Any) -> None:  # type: ignore[override]
        Base.initialize(self, route)
        tornado.web.StaticFileHandler.initialize(self, path=str(STATIC))

    def validate_absolute_path(self, root: str, absolute_path: str) -> str | None:
        if Path(absolute_path).suffix not in CONTENT_TYPES:
            raise error("not_found")
        return super().validate_absolute_path(root, absolute_path)

    def get_content_type(self) -> str:
        return CONTENT_TYPES[Path(self.absolute_path).suffix]

    def set_extra_headers(self, path: str) -> None:
        self.set_header("Cache-Control", "no-cache")


class Phone(Base):
    """``GET /api/phone/{pair}``: the simulated phone's inbox; needs no session at all.

    The phone page holds no cookie or tab and never creates or reads a session; it shows
    messages only for a pairing still on the allow list.
    """

    def get(self, pair: str) -> None:
        if not PAIR.fullmatch(pair):
            raise error("not_found")
        lang = self._choice("lang", LOCALES, "zh-CN")
        view = self._choice("view", VIEWS, "customer")
        store = self.settings["store"]
        allowed = store.pairs.allowed(pair)
        if allowed and public(self.settings) is not None:
            # The activity lease: a valid pairing's poll keeps its session in use (RW-8
            # item 3). The pairing is a bearer credential: whoever holds a valid one may read it.
            store.touch_pair(pair)
        locale, audience = set_locale(lang), set_audience(view)
        try:
            payload = self.settings["vm"].phone.payload(
                pair, allowed=allowed, public=public(self.settings) is not None
            )
        finally:
            reset_audience(audience)
            reset_locale(locale)
        self.respond(200, security.dumps(payload))

    def _choice(self, name: str, allowed: tuple[str, ...], default: str) -> str:
        values = self.request.query_arguments.get(name, [])
        if not values:
            return default
        value = values[0].decode("utf-8", "replace")
        if len(values) > 1 or value not in allowed:
            raise error("not_found")
        return value


# --- session requests ---------------------------------------------------------------------


@dataclass
class Call:
    """What one action sees: the session, the checked body and path, the modules."""

    ws: Any
    body: dict[str, Any]
    path: dict[str, str]
    features: frozenset[str]
    service: Any
    refresh: Any
    vm: Any
    live: Any
    # The phone pairing allow list: the authorization lab's child phone joins it (R2-S2).
    pairs: Any = None
    # The application's ``PublicConfig`` (None outside public mode), passed from the
    # settings so an action never has to guess it from the session (RW-8 item 5).
    public: Any = None

    def tab(self, name: str) -> dict[str, Any]:
        return {"name": name, "revision": self.ws.revision, "vm": self.vm.tabs.tab(self.ws, name)}


@dataclass(frozen=True)
class Action:
    run: Callable[[Call], Mapping[str, Any] | None]
    fields: Mapping[str, tuple[Check, bool]] = field(default_factory=dict)
    flow: str = "business"  # business | shell | live
    needs: tuple[str, ...] = ()
    check: Callable[[Call], None] | None = None
    tab: str | None = None
    sync: bool = True
    panel: bool = False
    # Still open after an unexpected failure distrusted the session.
    after_failure: bool = False


class Api(Base):
    def context(self, ws: Any, values: dict[str, Any], path: dict[str, str]) -> Call:
        settings = self.settings
        return Call(
            ws=ws,
            body=values,
            path=path,
            features=features(settings),
            service=settings["service"],
            refresh=settings["refresh"],
            vm=settings["vm"],
            live=settings["live"],
            pairs=settings["store"].pairs,
            public=public(settings),
        )

    def session(self) -> Any:
        """The session of this cookie and tab, or 401; the generation is checked later."""
        browser = security.browser_id(self.request.headers, public(self.settings))
        self.browser = browser
        if browser is None or self.tab is None:
            raise error("no_session")
        ws = self.settings["store"].lookup(browser, self.tab)
        if ws is None:
            raise error("no_session")
        self.ws = ws
        return ws

    def admit(self) -> Any:
        """Public mode: this request's place in flight, by client address and browser,
        taken before the executor is asked for a queue place (RW-8 item 2) and given back by
        ``readmit``; None outside public mode. Over either limit the answer is 503 ``busy``
        and no queue place, lock or controller is touched."""
        limits = self.settings.get("limits")
        if limits is None:
            return None
        held = limits.inflight.acquire(self.source or public_mode.UNKNOWN, self.browser)
        if held is None:
            raise error("busy")
        return held

    def readmit(self, held: Any) -> None:
        if held is not None:
            self.settings["limits"].inflight.release(held)

    def state(self, ws: Any) -> dict[str, Any]:
        with bound(ws):
            return self.settings["vm"].app.app_state(ws)

    def with_state(self, ws: Any, exc: WebError) -> tuple[int, bytes]:
        with bound(ws):
            payload = body(exc)
            state = self.settings["vm"].app.app_state(ws)
        payload.update({"generation": ws.generation, "revision": ws.revision, "state": state})
        _same_version(ws, state)
        return exc.status, security.dumps(payload)

    def guarded(self, ws: Any, work: Callable[[Any], Any], *, post: bool, check_generation: bool):
        """Runs in the pool thread, under the session lock."""
        if not self.settings["store"].current(ws):
            raise Moved()
        # The view models read the same capabilities the routes open (B2-1, Q-2).
        ws.features = features(self.settings)
        # A closed lab capability revokes a running experiment (R2-E7).
        lab.sync_capability(ws, post=post)
        if check_generation and ws.generation != self.generation:
            return self.with_state(ws, error("stale_generation"))
        if post:
            ws.revision += 1
        try:
            result = work(ws)
        except WebError as exc:
            if not exc.with_state:
                raise
            return self.with_state(ws, exc)
        return (200, security.dumps(result)) if isinstance(result, dict) else result

    async def locked(
        self,
        ws: Any,
        work: Callable[[Any], Any],
        *,
        pool: str = "main",
        post: bool = False,
        check_generation: bool = True,
    ) -> Any:
        store = self.settings["store"]
        held = self.admit()
        try:
            for _ in range(4):
                try:
                    return await self.settings["executor"].run(
                        ws,
                        lambda: self.guarded(
                            ws, work, post=post, check_generation=check_generation
                        ),
                        pool=pool,
                    )
                except Moved:
                    # Reset: the slot holds the new session, whose state answers this one.
                    ws = store.lookup(ws.browser_id, ws.tab_id)
                    if ws is None:
                        raise error("no_session") from None
                    self.ws = ws
            raise error("busy")
        finally:
            self.readmit(held)

    def path_values(self, names: tuple[str, ...], args: tuple[str, ...]) -> dict[str, str]:
        return dict(zip(names, args, strict=True))

    def check_path(self, path: Mapping[str, str]) -> None:
        for name, value in path.items():
            pattern = PARAMS.get(name)
            if pattern is not None and not pattern.fullmatch(value):
                raise error("not_found")
            if name == "name" and value not in TAB_NAMES:
                raise error("not_found")


class Read(Api):
    kind = "get"

    async def get(self, *args: str) -> None:
        read: Callable[[Call], Mapping[str, Any]] = self.options["read"]
        if self.route == "/api/rogue":
            # A public mechanism report has no banking session, generation or mutation.
            held = self.admit()
            try:
                result = await self.settings["executor"].call(lambda: read(None))
            finally:
                self.readmit(held)
            self.respond(200, security.dumps({"ok": True, **result}))
            return
        needs: tuple[str, ...] = self.options.get("needs", ())
        path = self.path_values(self.options.get("params", ()), args)
        ws = self.session()

        def work(ws: Any) -> dict[str, Any]:
            self.check_path(path)
            call = self.context(ws, {}, path)
            if not set(needs) <= call.features:
                raise error("not_found")
            return ok(ws, read(call))

        status, data = await self.locked(ws, work)
        self.respond(status, data)


class Write(Api):
    kind = "post"
    methods = ("POST",)

    async def post(self, *args: str) -> None:
        action: Action = self.options["action"]
        values = security.fields(security.parse(self.request.body), action.fields)
        path = self.path_values(self.options.get("params", ()), args)
        ws = self.session()
        live = self.settings["live"]
        pool = "live" if action.flow == "business" and live.may_call(ws) else "main"
        status, data = await self.locked(
            ws, lambda ws: self.perform(ws, action, values, path), pool=pool, post=True
        )
        self.respond(status, data)

    def perform(
        self, ws: Any, action: Action, values: dict[str, Any], path: dict[str, str]
    ) -> dict[str, Any]:
        self.check_path(path)
        call = self.context(ws, values, path)
        if not set(action.needs) <= call.features:
            raise error("not_found")
        if action.check is not None:
            action.check(call)
        if getattr(ws, "untrusted", False) and not action.after_failure:
            # After an unexpected failure only Cancel, Stop, Handoff and Reset remain.
            raise error("not_applicable")
        try:
            return self.run(ws, action, call)
        except WebError as exc:
            if exc.status >= 500 and not exc.contained:
                # An unknown or broken result after the request began (outcome_unknown,
                # internal): state may have changed, so nothing is trusted again (R2-E3).
                # Only a refusal below 500 is known to have changed nothing. The session
                # is distrusted first, before anything else can fail (RW-4'''').
                self.distrust(ws, call)
                _failed(self, type(exc))
            raise
        except Exception as exc:
            # The request may have changed state before it failed: nothing is trusted again.
            self.distrust(ws, call)
            _failed(self, type(exc))
            raise error("outcome_unknown") from None

    def run(self, ws: Any, action: Action, call: Call) -> dict[str, Any]:
        business = action.flow == "business"
        if business:
            call.live.sync(ws, apply=True)
        # The session's count of product calls that may change state, taken right before
        # the action and compared after a refusal (RW-5 item 2). The counter object is
        # held here and only grows, so nothing inside the action can lower or replace it.
        effects = ws.effects
        before = effects.count
        try:
            extras = dict(action.run(call) or {})
        except WebOutcomeUnknown as failure:
            # The call began: what it left behind is shown, and the task stays closed. The
            # call's own failure is raised again, whatever the closing steps do.
            self.close_failed(ws, call, business, failure)
            raise
        except WebError as exc:
            if exc.status >= 500 and not exc.contained:
                # An unknown or broken result: perform distrusts it.
                raise
            # Judged only while the counter taken above is still the session's and the
            # one all three proxies count into; otherwise the count says nothing, and the
            # result is unknown (RW-6 item 2, fail closed).
            unchanged = web_state.effects_bound(ws, effects) and effects.count == before
            if isinstance(exc, WebOperationFailed):
                # An expected failure keeps its status only when it carries the source
                # mark of a gesture of this session (RW-6 item 1), or when it was raised
                # before any product call could change state.
                kept = unchanged or (
                    web_state.effects_bound(ws, effects) and expected_failure(exc, ws)
                )
                if kept:
                    # The call began: what it left behind is shown, and the task stays
                    # closed. The failure is raised again, whatever the closing steps do.
                    self.close_failed(ws, call, business, exc)
                    raise
            elif unchanged:
                # A refusal raised before any product call could change state.
                raise
            # A refusal, a contained result or an unmarked expected failure raised after a
            # product call that may have changed state would say that nothing changed (or
            # that the call failed as expected): the result is unknown instead, whatever
            # raised it, and perform distrusts the session (RW-5 item 2, RW-6).
            _failed(self, type(exc))
            failure = WebOutcomeUnknown()
            self.close_failed(ws, call, business, failure)
            raise failure from None
        try:
            if business or (action.flow == "live" and action.sync):
                call.live.sync(ws, apply=True)
            with bound(ws):
                call.refresh.refresh(ws)
                state = call.vm.app.app_state(ws)
                if action.tab is not None:
                    extras["tab"] = call.tab(action.tab)
                if action.panel:
                    extras["live"] = call.live.view(ws)
            return ok(ws, extras, state)
        except WebError as exc:
            if exc.status >= 500 and not exc.contained:
                raise
            # The action ran and may have changed state: a refusal or a contained result
            # raised while closing it would say that nothing changed. Its result is
            # unknown instead, and the session is distrusted (RW-4'''').
            _failed(self, type(exc))
            raise WebOutcomeUnknown() from None

    def close_failed(self, ws: Any, call: Call, business: bool, failure: WebError) -> None:
        """Show what a call that began and failed left behind: the live sync, the refresh.

        A closing step that fails is only logged; it never replaces the call's own
        failure, which ``run`` raises again (RW-4''''). An unknown result therefore stays
        unknown and ``perform`` distrusts the session. After an expected failure
        (``operation_failed``) a failed closing step distrusts the session here, as any
        unexpected failure does.
        """
        try:
            if business:
                call.live.sync(ws, apply=True)
            with bound(ws):
                call.refresh.refresh(ws)
        except Exception as exc:
            _failed(self, type(exc))
            if isinstance(failure, WebOperationFailed):
                self.distrust(ws, call)

    def distrust(self, ws: Any, call: Call) -> None:
        """Revoke every token and gate record; no task of this session is offered again.

        Never raises: the session is marked and its tokens dropped before the refresh, and
        a refresh that fails is only logged (RW-4'''').
        """
        ws.untrusted = True
        for name in ("gestures", "gate_eval", "gates"):
            held = getattr(ws, name, None)
            if isinstance(held, dict):
                held.clear()
        try:
            with bound(ws):
                call.refresh.refresh(ws)
        except Exception as exc:
            _failed(self, type(exc))


class OpenSession(Api):
    """``POST /api/session``: reuse (browser, tab) when it exists, else a new session."""

    kind = "session"
    methods = ("POST",)
    FIELDS: Mapping[str, tuple[Check, bool]] = {
        "tab": (matching(TAB), False),
        "view": (one_of(*VIEWS), False),
        "locale": (one_of(*LOCALES), False),
    }

    async def post(self) -> None:
        config = public(self.settings)
        values = security.fields(security.parse(self.request.body), self.FIELDS)
        browser = security.browser_id(self.request.headers, config)
        self.browser = browser
        reply = None
        ws = (
            self.settings["store"].lookup(browser, values["tab"])
            if browser and "tab" in values
            else None
        )
        if ws is not None:
            self.ws = ws
            try:
                reply = await self.locked(ws, self.reuse, post=True, check_generation=False)
            except WebError as exc:
                if exc.code != "no_session":
                    raise
        if reply is None:
            held = self.admit()
            try:
                if config is not None:
                    # Only a new session draws from the address's session bucket.
                    limiter = self.settings["limits"].limiter
                    if not limiter.allow("session", self.source or public_mode.UNKNOWN):
                        raise error("busy")
                *reply, browser = await self.settings["executor"].call(
                    lambda: self.create(browser, values, client=self.source)
                )
            finally:
                self.readmit(held)
        attributes: dict[str, Any] = {"path": "/", "httponly": True, "samesite": "Strict"}
        if config is not None:
            # The ``__Host-`` cookie: Secure, Path=/ and no Domain (RW-8 item 12).
            attributes["secure"] = True
        self.set_cookie(security.cookie_name(config), browser, **attributes)
        self.respond(*reply)

    def reuse(self, ws: Any) -> dict[str, Any]:
        return ok(ws, {"tab": ws.tab_id}, self.state(ws))

    def create(
        self, browser: str | None, values: Mapping[str, Any], *, client: str | None = None
    ) -> tuple[int, bytes, str]:
        store = self.settings["store"]
        ws, browser, tab, _ = store.open(
            browser, None, values.get("view"), values.get("locale"), hold=True, client=client
        )
        self.ws = ws
        ws.features = features(self.settings)
        try:
            with bound(ws):
                self.settings["refresh"].refresh(ws)
                state = self.settings["vm"].app.app_state(ws)
            return 200, security.dumps(ok(ws, {"tab": tab}, state)), browser
        finally:
            store.release(ws)


class Reset(Api):
    """``POST /api/session/reset``: a new session in the same slot, a new generation."""

    kind = "post"
    methods = ("POST",)

    async def post(self) -> None:
        security.fields(security.parse(self.request.body), {})
        ws = self.session()
        store = self.settings["store"]
        result = await self.locked(ws, store.reset, post=True)
        if isinstance(result, tuple):
            self.respond(*result)
            return
        new = result
        self.ws = new

        def fresh(new: Any) -> dict[str, Any]:
            with bound(new):
                self.settings["refresh"].refresh(new)
            return ok(new, None, self.state(new))

        status, data = await self.locked(new, fresh, check_generation=False)
        self.respond(status, data)


# --- the actions -----------------------------------------------------------------------------


def _status(result: Any) -> dict[str, str]:
    # Only the status leaves the server, and only one of the known values: never ids,
    # cards, receipts or free text the result may carry.
    if not isinstance(result, Mapping) or not isinstance(result.get("status"), str):
        raise error("internal")
    if result["status"] not in STATUSES:
        raise WebOutcomeUnknown()
    return {"status": result["status"]}


def _rkey(value: Any) -> dict[str, str]:
    if not isinstance(value, str) or not RKEY.fullmatch(value):
        raise error("internal")
    return {"rkey": value}


def _text(call: Call) -> None:
    text = call.body["text"]
    if not 1 <= len(text) <= MAX_CHARS or not text.strip():
        raise error("invalid_input")


def _shortcut_open(call: Call) -> None:
    if "shortcut:" + call.body["shortcut"] not in call.features:
        raise error("not_found")


def _answers(call: Call) -> None:
    if not call.body["answers"]:
        raise error("invalid_input")


def _aa_count(call: Call) -> None:
    if not 1 <= len(call.body["choices"]) <= MAX_AA:
        raise error("invalid_input")


def _code(call: Call) -> None:
    # Only a six-digit string reaches the controller; never None (INV-2.2).
    code = call.body.get("code")
    if not isinstance(code, str) or not CODE.fullmatch(code):
        raise error("invalid_input")


def _nonce(call: Call) -> dict[str, Any]:
    nonce = call.live.challenge(call.ws)
    if not isinstance(nonce, str) or not NONCE.fullmatch(nonce):
        raise error("internal")
    answer: dict[str, Any] = {"nonce": nonce}
    if call.public is not None and call.public.kdf is not None:
        # Public mode: the verifier's public derivation parameters, so the browser proves
        # the verifier the configuration holds instead of the code (design §4.7).
        answer["kdf"] = call.public.kdf.wire()
    return answer


def _deployment_allowed(call: Call) -> None:
    # Public mode: only the site's deployment may be selected (RW-8 item 6), refused here
    # before the action, as any invalid input is.
    allowed = getattr(call.ws, "public_deployments", None)
    if allowed is not None and call.body["deployment"] not in allowed:
        raise error("invalid_input")


def _unlock(call: Call) -> dict[str, str]:
    result = call.live.unlock(call.ws, call.body["nonce"], call.body["proof"])
    if result not in UNLOCK_RESULTS:
        raise error("internal")
    return {"result": result}


def _gesture_fields(**extra: tuple[Check, bool]) -> dict[str, tuple[Check, bool]]:
    return {
        "gesture": (matching(GESTURE), True),
        "card_version": (matching(CARD_VERSION), True),
        **extra,
    }


CHOICE_FIELD = {"choice": (matching(CHOICE), True)}
LISTED_CHOICE = {**CHOICE_FIELD, "list_version": (matching(CARD_VERSION), True)}
ANSWERS = obj({
    "verified": (one_of("yes", "no", "unsure"), False),
    "guided": (one_of("no", "yes"), False),
})
SUBSCRIPTIONS = ("tab:accounts", "shortcut:subscriptions")
LAB_GENERATION = {"lab_generation": (matching(GENERATION), True)}
# The authorization lab: its own capability, the judge view only (R2-S1, R2-E7).
LAB = (lab.FEATURE,)


def _lab_open(call: Call) -> None:
    lab.open_to(call.ws)


def _lab_gesture_check(call: Call) -> None:
    lab.open_to(call.ws)
    if call.body["op"] == "mfa_verify":
        _code(call)


POSTS: dict[str, Action] = {
    "/api/lab/start": Action(
        lambda c: {"lab": lab.start(c.ws, c.body["scenario"], pairs=c.pairs)},
        {"scenario": (one_of("yellow", "red"), True), "mode": (one_of("scripted"), True)},
        flow="shell",
        needs=LAB,
        check=_lab_open,
    ),
    "/api/lab/change": Action(
        lambda c: {"lab": lab.change(c.ws, c.body["lab_generation"])},
        LAB_GENERATION,
        flow="shell",
        needs=LAB,
        check=_lab_open,
    ),
    "/api/lab/try-previous": Action(
        lambda c: {"lab": lab.try_previous(c.ws, c.body["lab_generation"])},
        LAB_GENERATION,
        flow="shell",
        needs=LAB,
        check=_lab_open,
    ),
    "/api/lab/gesture": Action(
        lambda c: {
            "lab": lab.gesture(
                c.ws,
                c.body["lab_generation"],
                **{key: value for key, value in c.body.items() if key != "lab_generation"},
            )
        },
        {
            **LAB_GENERATION,
            "tkey": (matching(TKEY), True),
            "op": (one_of("confirm", "mfa_request", "mfa_verify"), True),
            **_gesture_fields(code=(matching(CODE), False)),
        },
        flow="shell",
        needs=LAB,
        check=_lab_gesture_check,
    ),
    "/api/session/login": Action(
        lambda c: {"result": _status(c.service.login(c.ws))}, flow="shell"
    ),
    "/api/session/view": Action(
        lambda c: c.service.set_view(c.ws, c.body["view"]),
        {"view": (one_of(*VIEWS), True)},
        flow="shell",
        after_failure=True,
    ),
    "/api/session/locale": Action(
        lambda c: c.service.set_locale(c.ws, c.body["locale"]),
        {"locale": (one_of(*LOCALES), True)},
        flow="shell",
        after_failure=True,
    ),
    "/api/chat/message": Action(
        lambda c: _rkey(
            c.service.send_message(
                c.ws, c.body["text"], c.body["external_files"], c.body["client_msg_id"]
            )
        ),
        {
            "text": (string, True),
            "external_files": (flag, True),
            "client_msg_id": (matching(CLIENT_MSG_ID), True),
        },
        check=_text,
    ),
    "/api/chat/shortcut": Action(
        lambda c: _rkey(c.service.send_shortcut(c.ws, c.body["shortcut"], c.body["client_msg_id"])),
        {
            "shortcut": (one_of(*SHORTCUTS), True),
            "client_msg_id": (matching(CLIENT_MSG_ID), True),
        },
        check=_shortcut_open,
    ),
    "/api/chat/responses/{rkey}/current": Action(
        lambda c: c.service.make_current(c.ws, c.path["rkey"])
    ),
    "/api/chat/responses/{rkey}/recipient": Action(
        lambda c: c.service.select_recipient(c.ws, c.path["rkey"], c.body["choice"]),
        CHOICE_FIELD,
    ),
    "/api/chat/responses/{rkey}/subscription": Action(
        lambda c: _rkey(
            c.service.choose_subscription(
                c.ws, c.path["rkey"], c.body["choice"], c.body["list_version"]
            )
        ),
        LISTED_CHOICE,
    ),
    "/api/chat/responses/{rkey}/schedule-cancel": Action(
        lambda c: _rkey(
            c.service.choose_schedule_cancel(
                c.ws, c.path["rkey"], c.body["choice"], c.body["list_version"]
            )
        ),
        LISTED_CHOICE,
    ),
    # A model suggestion is sent as the canonical sentence the server wrote, like typing it.
    "/api/chat/responses/{rkey}/suggestion": Action(
        lambda c: _rkey(
            suggest.choose(
                c.ws,
                c.path["rkey"],
                c.body["choice"],
                c.body["list_version"],
                c.body["client_msg_id"],
            )
        ),
        {**LISTED_CHOICE, "client_msg_id": (matching(CLIENT_MSG_ID), True)},
    ),
    "/api/chat/responses/{rkey}/gate/answer": Action(
        lambda c: c.service.gate_answer(
            c.ws, c.path["rkey"], c.body["gate_version"], c.body["answers"]
        ),
        {"gate_version": (matching(GATE_VERSION), True), "answers": (ANSWERS, True)},
        check=_answers,
    ),
    "/api/chat/responses/{rkey}/gate/retry": Action(
        lambda c: c.service.gate_retry(c.ws, c.path["rkey"], c.body["gate_version"]),
        {"gate_version": (matching(GATE_VERSION), True)},
    ),
    "/api/chat/responses/{rkey}/birthday": Action(
        lambda c: c.service.choose_birthday(c.ws, c.path["rkey"], c.body["choice"]),
        CHOICE_FIELD,
        needs=("plan_select",),
    ),
    "/api/chat/responses/{rkey}/aa": Action(
        lambda c: _rkey(c.service.complete_aa(c.ws, c.path["rkey"], c.body["choices"])),
        {"choices": (distinct(CHOICE, 1000), True)},
        needs=("aa",),
        check=_aa_count,
    ),
    "/api/chat/stop": Action(lambda c: _rkey(c.service.chat_stop(c.ws)), after_failure=True),
    "/api/chat/handoff": Action(lambda c: _rkey(c.service.handoff(c.ws)), after_failure=True),
    "/api/chat/compensate": Action(
        lambda c: c.service.compensate_latest(c.ws, c.body["plan_version"]),
        {"plan_version": (matching(CARD_VERSION), True)},
        needs=("compensate",),
    ),
    "/api/device/tasks/{tkey}/nodes": Action(
        lambda c: c.service.select_nodes(
            c.ws, c.path["tkey"], c.body["card_version"], c.body["nodes"]
        ),
        {"card_version": (matching(CARD_VERSION), True), "nodes": (distinct(NODE, 1000), True)},
        needs=("plan_select",),
    ),
    "/api/device/tasks/{tkey}/confirm": Action(
        lambda c: {
            "result": _status(
                c.service.device_confirm(
                    c.ws, c.path["tkey"], c.body["gesture"], c.body["card_version"]
                )
            )
        },
        _gesture_fields(),
    ),
    "/api/device/tasks/{tkey}/mfa/request": Action(
        lambda c: {
            "result": _status(
                c.service.device_mfa_request(
                    c.ws, c.path["tkey"], c.body["gesture"], c.body["card_version"]
                )
            )
        },
        _gesture_fields(),
    ),
    "/api/device/tasks/{tkey}/mfa/verify": Action(
        lambda c: {
            "result": _status(
                c.service.device_mfa_verify(
                    c.ws, c.path["tkey"], c.body["gesture"], c.body["card_version"], c.body["code"]
                )
            )
        },
        _gesture_fields(code=(anything, False)),
        check=_code,
    ),
    "/api/device/tasks/{tkey}/cancel": Action(
        lambda c: c.service.device_cancel(c.ws, c.path["tkey"]), after_failure=True
    ),
    "/api/stop": Action(lambda c: c.service.stop_all(c.ws), after_failure=True),
    "/api/subscriptions/query": Action(
        lambda c: c.service.subscriptions_query(c.ws), needs=SUBSCRIPTIONS, tab="accounts"
    ),
    "/api/subscriptions/cancel": Action(
        lambda c: c.service.subscriptions_cancel(c.ws, c.body["choice"], c.body["list_version"]),
        LISTED_CHOICE,
        needs=SUBSCRIPTIONS,
        tab="accounts",
    ),
    "/api/billing/analyze": Action(
        lambda c: c.service.billing_analyze(c.ws, c.body.get("period")),
        {"period": (matching(PERIOD), False)},
        needs=("tab:accounts", "shortcut:billing"),
        tab="accounts",
    ),
    "/api/wealth/compare": Action(
        lambda c: c.service.wealth_compare(c.ws, c.body["objective"]),
        {"objective": (one_of("net_return", "total_fee", "liquidity"), True)},
        needs=("tab:wealth",),
        tab="wealth",
    ),
    "/api/wealth/purchase": Action(
        lambda c: c.service.wealth_purchase(c.ws, c.body["choice"], c.body["list_version"]),
        {
            "choice": (nullable(matching(CHOICE)), True),
            "list_version": (matching(CARD_VERSION), True),
        },
        needs=("tab:wealth",),
        tab="wealth",
    ),
    "/api/wealth/redeem": Action(
        lambda c: c.service.wealth_redeem(c.ws, c.body["choice"], c.body["list_version"]),
        LISTED_CHOICE,
        needs=("tab:wealth",),
        tab="wealth",
    ),
    "/api/presets/suitability-r4": Action(
        lambda c: c.service.preset_r4(c.ws), needs=("tab:wealth",), tab="wealth"
    ),
    "/api/live/challenge": Action(_nonce, flow="live", needs=("live",), sync=False),
    "/api/live/unlock": Action(
        _unlock,
        {"nonce": (matching(NONCE), True), "proof": (matching(PROOF), True)},
        flow="live",
        needs=("live",),
        panel=True,
    ),
    "/api/live/select": Action(
        lambda c: c.live.select(c.ws, c.body["deployment"]),
        {"deployment": (matching(DEPLOYMENT), True)},
        flow="live",
        needs=("live",),
        check=_deployment_allowed,
        panel=True,
    ),
    "/api/live/toggle": Action(
        lambda c: c.live.toggle(c.ws, c.body["on"]),
        {"on": (boolean, True)},
        flow="live",
        needs=("live",),
        panel=True,
    ),
}


def _tab(call: Call) -> dict[str, Any]:
    name = call.path["name"]
    if name != "operator" and "tab:" + name not in call.features:
        raise error("not_found")
    return {"tab": call.tab(name)}


def _evidence(call: Call) -> dict[str, Any]:
    rkey = call.path["rkey"]
    if rkey not in call.ws.rkeys:
        raise error("not_found")
    # Evidence is a judge-view display preference, not access control (INV-5.1).
    technical = call.vm.evidence.technical(call.ws, rkey) if call.ws.view == "judge" else None
    return (
        {"available": True, "technical": technical}
        if technical is not None
        else {"available": False}
    )


def _lab_view(call: Call) -> dict[str, Any]:
    lab.open_to(call.ws)
    return {"lab": lab.view(call.ws)}


GETS: dict[str, tuple[Callable[[Call], Mapping[str, Any]], tuple[str, ...]]] = {
    "/api/rogue": (lambda c: {"rogue": rogue.report()}, ()),
    # The lab's SMS is read only by its own pairing's phone page (R2-S2): no lab phone GET.
    "/api/lab": (_lab_view, LAB),
    "/api/state": (lambda c: {"state": c.vm.app.app_state(c.ws)}, ()),
    "/api/i18n": (
        lambda c: {
            "locale": c.ws.locale,
            "view": c.ws.view,
            "strings": c.vm.app.i18n_strings(c.ws),
        },
        (),
    ),
    "/api/tabs/{name}": (_tab, ()),
    "/api/evidence/responses/{rkey}": (_evidence, ()),
    "/api/live": (lambda c: {"live": c.live.view(c.ws)}, ("live",)),
}
