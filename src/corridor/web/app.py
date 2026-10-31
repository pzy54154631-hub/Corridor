"""``make_app``: the route table, the session store, the executor and their modules.

``service``, ``refresh``, ``vm``, ``live`` and ``state`` are module objects and default to
``corridor.web.<name>``; tests may pass doubles to exercise the pipeline alone.
"""

import re
from typing import Any

import tornado.web

from corridor.web import handlers, lab, logs
from corridor.web import public as public_mode
from corridor.web.executor import Executor
from corridor.web.sessions import SessionStore

# Two limits (R2-E12). The application parses at most 64 KiB (the contract's limit): a
# larger body gets the contract's 413. The entry buffers at most 128 KiB per connection:
# a body declared or streamed beyond that is not read, and the connection is answered
# 400 by the HTTP server and closed. A body that trickles in, or a connection left idle,
# is closed after a bounded time. Budget: 128 KiB per open connection.
ENTRY_LIMIT = 128 * 1024
SERVER_OPTIONS: dict[str, Any] = {
    "max_body_size": ENTRY_LIMIT,
    "max_buffer_size": ENTRY_LIMIT,
    "body_timeout": 30.0,
    "idle_connection_timeout": 120.0,
    "xheaders": False,
    "decompress_request": False,
}
MODULES = ("service", "refresh", "vm", "live", "state")


def _default(name: str) -> Any:
    if name == "service":
        from corridor.web import service

        return service
    if name == "refresh":
        from corridor.web import refresh

        return refresh
    if name == "live":
        from corridor.web import live

        return live
    if name == "state":
        from corridor.web import state

        return state
    from corridor.web import vm
    from corridor.web.vm import app, evidence, phone, tabs  # noqa: F401 - loads the submodules

    return vm


def _pattern(template: str) -> tuple[str, tuple[str, ...]]:
    names = tuple(re.findall(r"\{(\w+)\}", template))
    pattern = re.escape(template)
    for name in names:
        pattern = pattern.replace(re.escape("{" + name + "}"), "([^/]+)")
    return pattern + "$", names


def routes() -> list[tuple[str, type, dict[str, Any]]]:
    table: list[tuple[str, type, dict[str, Any]]] = [
        (r"/api/session$", handlers.OpenSession, {"route": "/api/session"}),
        (r"/api/session/reset$", handlers.Reset, {"route": "/api/session/reset"}),
    ]
    for template, action in handlers.POSTS.items():
        pattern, names = _pattern(template)
        table.append((
            pattern,
            handlers.Write,
            {"route": template, "action": action, "params": names},
        ))
    for template, (read, needs) in handlers.GETS.items():
        pattern, names = _pattern(template)
        options = {"route": template, "read": read, "needs": needs, "params": names}
        table.append((pattern, handlers.Read, options))
    table += [
        (r"/api/phone/([^/]+)$", handlers.Phone, {"route": "/api/phone/{pair}"}),
        (r"/api/.*", handlers.Missing, {"route": "/api/*"}),
        (r"/$", handlers.Page, {"route": "/", "file": "index.html"}),
        (r"/phone/([^/]+)$", handlers.Page, {"route": "/phone/{pair}", "file": "phone.html"}),
        (r"/vendor/corridor-hmac\.js$", handlers.Vendor, {"route": "/vendor/corridor-hmac.js"}),
        (r"/static/(.+)$", handlers.Static, {"route": "/static/*"}),
        (r"/([^?#]+\.(?:js|css|svg|png|woff2|ico))$", handlers.Static, {"route": "/*"}),
    ]
    return table


def make_app(settings: dict[str, Any] | None = None) -> tornado.web.Application:
    """Settings (all optional): ``port`` (required to answer any request), ``max_sessions``,
    ``max_tabs_per_browser``, ``pool_main``, ``pool_live``, ``queue_main``, ``queue_live``,
    ``features``, the module objects ``service``, ``refresh``, ``vm``, ``live``, ``state``,
    and for public mode ``public`` (a ``public.PublicConfig``; None, the default, is the
    local service with nothing added) and ``clock`` (its monotonic clock; tests inject one).
    """
    options = dict(settings or {})
    modules = {name: options.get(name) or _default(name) for name in MODULES}
    public = options.get("public")
    limits = None
    clock = None
    if public is not None:
        clock = options.get("clock") or public_mode.now
        limits = public_mode.Limits(public, clock=clock)
    executor = Executor(
        pool_main=options.get("pool_main", 4),
        pool_live=options.get("pool_live", 2),
        queue_main=options.get("queue_main", 16),
        queue_live=options.get("queue_live", 4),
    )
    store = SessionStore(
        state=lab.LifecycleState(modules["state"]),
        live=modules["live"],
        max_sessions=options.get("max_sessions", 8 if public is None else public.max_sessions),
        max_tabs_per_browser=options.get("max_tabs_per_browser", 4),
        public=public,
        clock=clock,
    )
    features = options.get("features")
    return tornado.web.Application(
        routes(),
        default_handler_class=handlers.Missing,
        default_handler_args={"route": logs.UNMATCHED},
        log_function=logs.access,
        debug=False,
        autoreload=False,
        serve_traceback=False,
        compress_response=False,
        xsrf_cookies=False,
        port=options.get("port"),
        features=None if features is None else frozenset(features),
        store=store,
        executor=executor,
        public=public,
        limits=limits,
        **modules,
    )
