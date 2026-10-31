"""Error codes of the web API (contract §2).

A response names only the code, a generic localized message and a retry hint: never an
exception text, a path, a traceback, a verification code, an access code or a proof.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Code:
    status: int
    with_state: bool
    retry: bool = False


CODES: dict[str, Code] = {
    "bad_json": Code(400, False),
    "unknown_field": Code(400, False),
    "no_session": Code(401, False, retry=True),
    "forbidden_origin": Code(403, False),
    "not_found": Code(404, True),
    "method_not_allowed": Code(405, False),
    "stale_generation": Code(409, True),
    "not_current": Code(409, True),
    "gesture_unknown": Code(409, True),
    "gesture_mismatch": Code(409, True),
    "card_changed": Code(409, True),
    "gesture_closed": Code(409, True),
    "facts_incomplete": Code(409, True),
    "gate_stale": Code(409, True),
    "not_applicable": Code(409, True),
    "msg_id_conflict": Code(409, True),
    "not_at_boundary": Code(409, True),
    "live_on": Code(409, True),
    "live_unavailable": Code(409, True),
    "too_large": Code(413, False),
    "invalid_input": Code(422, True),
    "operation_failed": Code(422, True),
    "outcome_unknown": Code(500, True),
    "internal": Code(500, False),
    "busy": Code(503, False, retry=True),
}

# Generic shell copy per code; every other code reads the generic line.
MESSAGE_KEYS = {
    "no_session": "web.ui.err.session",
    "stale_generation": "web.ui.err.stale_generation",
    "not_current": "web.ui.err.not_current",
    "gesture_unknown": "web.ui.err.gesture",
    "gesture_mismatch": "web.ui.err.gesture",
    "card_changed": "web.ui.err.card_changed",
    "outcome_unknown": "web.ui.err.unknown",
    "busy": "web.ui.err.busy",
}
GENERIC = "web.ui.err.generic"


class WebError(Exception):
    code: str
    status: int
    with_state: bool
    retry: bool
    # True only for a failure confined to an isolated child session (the authorization
    # lab): the child is closed, the parent's own state was not touched (R2-E7).
    contained: bool = False

    def __init__(self, code: str) -> None:
        spec = CODES[code]
        super().__init__(code)
        self.code = code
        self.status = spec.status
        self.with_state = spec.with_state
        self.retry = spec.retry


class WebOperationFailed(WebError):
    """An expected failure after the call began: recorded on the reply, the task closed.

    ``origin`` is the source mark: only ``live.operation`` sets it, to the gesture (its
    ``Op``) that raised the failure, and that gesture holds the failure in turn. One
    without the mark, or bound to another gesture or session, is not an expected failure
    (RW-6 item 1): after a product call that may have changed state, the handler answers
    it as an unknown result.
    """

    def __init__(self, origin: object = None) -> None:
        super().__init__("operation_failed")
        self.origin = origin


class WebOutcomeUnknown(WebError):
    """An unexpected failure after the call began: the task closes as outcome unknown."""

    def __init__(self) -> None:
        super().__init__("outcome_unknown")


class WebChildOutcomeUnknown(WebError):
    """An unknown result inside the authorization lab's child session. The child is
    closed and its cached gestures dropped; the parent session is not distrusted, because
    the child's controllers share no state with the parent's."""

    contained = True

    def __init__(self) -> None:
        super().__init__("outcome_unknown")


class WebInvariantError(WebError):
    """A broken invariant before any controller call; no state is attached."""

    def __init__(self) -> None:
        super().__init__("internal")


_CLASSES: dict[str, type[WebError]] = {
    "operation_failed": WebOperationFailed,
    "outcome_unknown": WebOutcomeUnknown,
    "internal": WebInvariantError,
}


def error(code: str) -> WebError:
    if code not in CODES:
        raise KeyError(code)
    special = _CLASSES.get(code)
    return special() if special is not None else WebError(code)


def message(code: str) -> str:
    """The generic text for ``code`` in the caller's locale and audience."""
    try:
        from corridor.web.catalog import wtr
    except Exception:
        # An error answer must not fail itself; the catalog has its own tests.
        return code
    for key in (MESSAGE_KEYS.get(code, GENERIC), GENERIC):
        try:
            return wtr(key)
        except (KeyError, ValueError, IndexError):
            continue
    return code


def body(exc: WebError) -> dict:
    return {
        "ok": False,
        "error": {"code": exc.code, "message": message(exc.code), "retry": exc.retry},
    }
