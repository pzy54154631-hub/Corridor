"""Transport checks, request bodies and response encoding for the web API.

Host, Origin, the JSON content type and the custom headers stop cross-origin pages
(including other local ports) and DNS rebinding. They do not authenticate the local
machine: any local process can still talk to the service (design §2.4.1).
"""

import json
import re
from collections.abc import Callable, Mapping
from typing import Any

from corridor.web.errors import error

MAX_BODY = 64 * 1024
COOKIE = "corridor_web"
# Public mode's cookie (RW-8 item 12): the ``__Host-`` prefix makes a browser accept it only
# with Secure, Path=/ and no Domain, so a sibling host of a shared domain cannot plant one.
HOST_COOKIE = "__Host-" + COOKIE

BROWSER = re.compile(r"[A-Za-z0-9_-]{43}")
TAB = re.compile(r"[A-Za-z0-9_-]{32}")
GENERATION = re.compile(r"[A-Za-z0-9_-]{12}")
RKEY = re.compile(r"R[1-9][0-9]{0,5}")
TKEY = re.compile(r"T[RB][1-9][0-9]{0,5}")
CHOICE = re.compile(r"c(?:0|[1-9][0-9]{0,3})")
NODE = re.compile(r"n(?:0|[1-9][0-9]{0,2})")
GATE_VERSION = re.compile(r"[A-Za-z0-9_-]{16}")
GESTURE = re.compile(r"[A-Za-z0-9_-]{22}")
CARD_VERSION = re.compile(r"[0-9a-f]{32}")
CLIENT_MSG_ID = re.compile(r"[A-Za-z0-9_-]{8,64}")
PAIR = re.compile(r"[A-Za-z0-9_-]{16,64}")
CODE = re.compile(r"[0-9]{6}")
NONCE = re.compile(r"[0-9a-f]{32}")
PROOF = re.compile(r"[0-9a-f]{64}")
PERIOD = re.compile(r"[0-9]{4}-[0-9]{2}")
DEPLOYMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
SURROGATE = re.compile(r"[\ud800-\udfff]")

VIEWS = ("customer", "judge")
LOCALES = ("zh-CN", "en")
SHORTCUTS = ("transfer", "billing", "wealth", "cards", "subscriptions", "birthday", "scam_drill")

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; frame-src 'self'; frame-ancestors 'self'; base-uri 'none'; "
    "form-action 'none'; object-src 'none'"
)
RESPONSE_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}
# A repeated copy of any of these is malformed, whatever the route.
SINGLE = (
    "Host",
    "Origin",
    "Content-Type",
    "Cookie",
    "Sec-Fetch-Site",
    "X-Corridor-Web",
    "X-Corridor-Tab",
    "X-Corridor-Generation",
)


def hosts(port: int | None) -> frozenset[str]:
    if port is None:
        return frozenset()
    return frozenset(f"{name}:{port}" for name in ("127.0.0.1", "localhost", "[::1]"))


def allowed(port: int | None, public: Any) -> tuple[frozenset[str], str]:
    """The Host values and the Origin scheme a request may carry: the loopback spellings
    with the port over ``http``, or in public mode the configured name alone (no port, as
    browsers send it for 443) over ``https`` (RW-7 item 3)."""
    if public is None:
        return hosts(port), "http"
    return frozenset({public.host}), "https"


def cookie_name(public: Any) -> str:
    return COOKIE if public is None else HOST_COOKIE


def single(headers: Any, name: str) -> str | None:
    values = headers.get_list(name)
    if len(values) > 1:
        raise error("forbidden_origin")
    return values[0] if values else None


def _json_type(value: str | None) -> bool:
    if value is None:
        return False
    media, *params = (part.strip() for part in value.split(";"))
    if media.lower() != "application/json":
        return False
    return not params or (
        len(params) == 1
        and params[0].replace(" ", "").lower() in ("charset=utf-8", 'charset="utf-8"')
    )


def check(
    headers: Any, port: int | None, *, kind: str, public: Any = None
) -> tuple[str | None, str | None]:
    """Validate the transport headers of one request; returns (tab, generation).

    ``kind`` is ``page`` (static files and the phone), ``get`` (session API reads),
    ``post`` (session API writes) or ``session`` (``POST /api/session``). ``public`` (the
    ``PublicConfig``) changes only which Host values and Origin scheme pass (``allowed``).
    """
    for name in SINGLE:
        single(headers, name)
    allowed_hosts, scheme = allowed(port, public)
    host = single(headers, "Host")
    if host is None or host not in allowed_hosts:
        raise error("forbidden_origin")
    if kind == "page":
        return None, None
    fetch_site = single(headers, "Sec-Fetch-Site")
    if fetch_site is not None and fetch_site != "same-origin":
        raise error("forbidden_origin")
    writes = kind in ("post", "session")
    origin = single(headers, "Origin")
    if (origin is None and writes) or (origin is not None and origin != scheme + "://" + host):
        raise error("forbidden_origin")
    if writes and not _json_type(single(headers, "Content-Type")):
        raise error("forbidden_origin")
    if kind == "session":
        if single(headers, "X-Corridor-Web") != "1":
            raise error("forbidden_origin")
        return None, None
    tab = single(headers, "X-Corridor-Tab")
    generation = single(headers, "X-Corridor-Generation")
    if tab is None:
        # A missing tab means no session (401), decided when the session is looked up.
        return None, None
    if not TAB.fullmatch(tab) or generation is None or not GENERATION.fullmatch(generation):
        raise error("forbidden_origin")
    return tab, generation


def browser_id(headers: Any, public: Any = None) -> str | None:
    """The browser cookie, or None when absent or not one this service could have set.

    Public mode reads only its own ``__Host-`` cookie; the plain one is not this service's.
    """
    raw = single(headers, "Cookie")
    if raw is None:
        return None
    wanted = cookie_name(public)
    found = []
    for part in raw.split(";"):
        name, _, value = part.strip().partition("=")
        if name == wanted:
            found.append(value.strip())
    if len(found) > 1:
        raise error("forbidden_origin")
    return found[0] if found and BROWSER.fullmatch(found[0]) else None


# --- request bodies -------------------------------------------------------------------------


def _refuse(value: str) -> Any:
    raise ValueError("non-integer number")


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _strings_ok(value: Any) -> bool:
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            if SURROGATE.search(item):
                return False
        elif isinstance(item, dict):
            stack.extend(item)
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return True


def parse(raw: bytes) -> dict[str, Any]:
    """At most 64 KiB of UTF-8 JSON: one object, integers only, no repeated keys."""
    if len(raw) > MAX_BODY:
        raise error("too_large")
    try:
        value = json.loads(
            raw.decode("utf-8"),
            parse_float=_refuse,
            parse_constant=_refuse,
            object_pairs_hook=_unique,
        )
    except (ValueError, RecursionError):
        raise error("bad_json") from None
    if not isinstance(value, dict) or not _strings_ok(value):
        raise error("bad_json")
    return value


Check = Callable[[Any], Any]


def _bad() -> Exception:
    return error("bad_json")


def string(value: Any) -> str:
    if not isinstance(value, str):
        raise _bad()
    return value


def anything(value: Any) -> Any:
    return value


def matching(pattern: re.Pattern[str]) -> Check:
    def check(value: Any) -> str:
        if not isinstance(value, str) or not pattern.fullmatch(value):
            raise _bad()
        return value

    return check


def one_of(*allowed: str) -> Check:
    def check(value: Any) -> str:
        if not isinstance(value, str) or value not in allowed:
            raise _bad()
        return value

    return check


def flag(value: Any) -> int:
    if type(value) is not int or value not in (0, 1):
        raise _bad()
    return value


def boolean(value: Any) -> bool:
    if not isinstance(value, bool):
        raise _bad()
    return value


def nullable(inner: Check) -> Check:
    def check(value: Any) -> Any:
        return None if value is None else inner(value)

    return check


def distinct(pattern: re.Pattern[str], limit: int) -> Check:
    """A list of distinct keys, order kept; counts beyond the format are the caller's."""

    def check(value: Any) -> list[str]:
        if not isinstance(value, list) or len(value) > limit:
            raise _bad()
        items = [matching(pattern)(item) for item in value]
        if len(set(items)) != len(items):
            raise _bad()
        return items

    return check


def obj(spec: Mapping[str, tuple[Check, bool]]) -> Check:
    def check(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise _bad()
        return fields(value, spec)

    return check


def fields(body: Mapping[str, Any], spec: Mapping[str, tuple[Check, bool]]) -> dict[str, Any]:
    """Whitelisted fields only (400 unknown_field), each of the declared type (400 bad_json)."""
    if set(body) - set(spec):
        raise error("unknown_field")
    result = {}
    for name, (check_value, required) in spec.items():
        if name in body:
            result[name] = check_value(body[name])
        elif required:
            raise _bad()
    return result


# --- responses ------------------------------------------------------------------------------


def _plain(value: Any) -> None:
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            if not all(isinstance(key, str) for key in item):
                raise error("internal")
            stack.extend(item.values())
        elif isinstance(item, (list, tuple)):
            stack.extend(item)
        elif isinstance(item, float):
            # Amounts and ratios travel as strings; a float here is a view-model bug.
            raise error("internal")
        elif item is not None and not isinstance(item, (str, int)):
            raise error("internal")


def dumps(payload: Mapping[str, Any]) -> bytes:
    _plain(payload)
    text = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    return text.encode("utf-8")
