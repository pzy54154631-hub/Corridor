"""Request-local display audience; a presentation preference, never access control.

The customer view hides evidence that the judge view shows. Neither audience is an
input to canonical facts, signatures, tiers, authorization or dispatch.
"""

from contextvars import ContextVar, Token

AUDIENCES = ("judge", "customer")
_audience: ContextVar[str] = ContextVar("corridor_display_audience", default="judge")


def get_audience() -> str:
    """Return this context's audience; library calls and tests default to the judge view."""
    value = _audience.get()
    return value if value in AUDIENCES else "judge"


def set_audience(audience: str) -> Token[str]:
    """Select an audience for this context without affecting concurrent sessions."""
    if audience not in AUDIENCES:
        raise ValueError("Unsupported display audience")
    return _audience.set(audience)


def reset_audience(token: Token[str]) -> None:
    """Restore the caller's context after a temporary display override."""
    _audience.reset(token)


def is_customer() -> bool:
    return get_audience() == "customer"
