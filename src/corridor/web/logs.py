"""Logging that never records identifiers, bodies or exception text.

An access line is the method, the route template and the status code: never the URI,
the query string, headers, cookies, the body or timings. A failure is logged by its
exception class name only, never by its message or traceback.
"""

import logging
from typing import Any

access_log = logging.getLogger("tornado.access")
app_log = logging.getLogger("tornado.application")

METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"})
UNMATCHED = "<unmatched>"


def route_of(handler: Any) -> str:
    return getattr(handler, "route", None) or UNMATCHED


def _method(handler: Any) -> str:
    method = handler.request.method
    return method if method in METHODS else "OTHER"


def access(handler: Any) -> None:
    """``Application(log_function=access)``."""
    status = handler.get_status()
    level = logging.INFO if status < 400 else logging.WARNING if status < 500 else logging.ERROR
    access_log.log(level, "%s %s %d", _method(handler), route_of(handler), status)


def failure(handler: Any, kind: type[BaseException]) -> None:
    app_log.error("%s %s failed: %s", _method(handler), route_of(handler), kind.__name__)
