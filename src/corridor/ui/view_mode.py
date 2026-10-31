"""Customer and judge views: a display preference, never access control.

The customer view does not render rule IDs, hashes, tokens or raw JSON; the judge
view shows every piece of evidence and is one click (or ``?view=judge``) away.
Neither view changes canonical facts, approvals, tiers, dispatch or audit.
"""

import os
from contextvars import ContextVar

import streamlit as st

from corridor.i18n import set_locale
from corridor.ui.audience import AUDIENCES, get_audience, set_audience
from corridor.ui.messages import tr

SESSION_KEY = "polish_view"
CUSTOMER_HIDDEN_PAGES = frozenset({"dag", "audit"})
_run_claims: ContextVar[frozenset[str]] = ContextVar("corridor_run_claims", default=frozenset())

_BASE_CSS = """
<style>
[data-testid="InputInstructions"] { display:none !important; }
.sim-phone-frame { max-width:390px; margin:0 auto; border:10px solid #2b2236;
border-radius:36px; background:#f4f1f8; padding:14px 14px 18px; min-height:640px; }
.sim-phone-status { display:flex; justify-content:space-between; font-size:12px;
color:#5d536b; padding:0 6px 10px; }
.sim-phone-title { font-weight:700; font-size:16px; text-align:center; margin-bottom:10px; }
.sms-bubble { background:#fff; border-radius:16px; padding:12px 14px; margin:8px 0;
box-shadow:0 3px 12px #2b223614; font-size:14px; line-height:1.6; }
.sms-bubble.sms-latest { border:2px solid #584174; }
.sms-bubble.sms-closed { color:#81788e; background:#efecf3; }
.sms-sender { font-size:12px; color:#766f83; margin-bottom:4px; }
.sms-status { font-size:12px; color:#81788e; margin-top:4px; }
.sim-phone-footer { font-size:11px; color:#81788e; text-align:center; margin-top:14px; }
.suit-block { border:1px solid #d9d2e4; border-radius:14px; background:#faf8fc;
padding:12px 16px; margin:8px 0 12px; font-size:13px; line-height:1.7; }
.suit-title { font-weight:650; margin-bottom:4px; }
.suit-footer { color:#766f83; font-size:12px; margin-top:6px; }
.fraud-hint { border:1px solid #ecc77a; border-radius:14px; background:#fff6e2;
color:#6b4b10; padding:12px 16px; margin:8px 0 12px; font-size:13px; line-height:1.7; }
.fraud-title { font-weight:700; margin-bottom:4px; }
.fraud-footer { color:#7a6234; font-size:12px; margin-top:6px; }
.fraud-judge { color:#7a6234; font-size:12px; margin-top:6px; font-style:italic; }
.scam-title { font-weight:700; font-size:14px; color:#1f3a5f; border-left:5px solid #3b6ea5;
padding-left:10px; }
.scam-sub { color:#4c5b6e; font-size:12px; margin:4px 0 10px 15px; }
.scam-facts { border:1px dashed #9fb6cf; border-radius:10px; background:#f3f7fb; color:#1f3a5f;
padding:10px 14px; margin:6px 0 10px; font-size:13px; line-height:1.7; }
.scam-facts-title { font-weight:650; margin-bottom:4px; }
.scam-cues { color:#6b4b10; background:#fff6e2; border-radius:10px; padding:8px 12px;
margin:0 0 10px; font-size:13px; }
.scam-questions { font-weight:650; font-size:13px; color:#1f3a5f; margin:6px 0 2px; }
.scam-pause { border:2px solid #b23b3b; border-radius:12px; background:#fff1f1; color:#6a1b1b;
padding:12px 16px; margin:10px 0; font-size:13px; line-height:1.7; }
.scam-pause-title { font-weight:700; margin-bottom:4px; }
.scam-footer { color:#4c5b6e; font-size:12px; margin-top:8px; }
.scam-judge { color:#4c5b6e; font-size:12px; margin-top:6px; font-style:italic; }
</style>
"""

_CUSTOMER_CSS = """
<style>
.bank-subtitle { display:none; }
.st-key-domestic_chatcol, .st-key-im_chatcol, .st-key-demo_chatcol {
max-width:420px; margin-left:auto; margin-right:auto; }
.st-key-domestic_chatcol [data-testid="stMetricValue"],
.st-key-im_chatcol [data-testid="stMetricValue"],
.st-key-demo_chatcol [data-testid="stMetricValue"] { font-size:1.1rem; }
.st-key-domestic_chatcol [data-testid="stMetricValue"] *,
.st-key-im_chatcol [data-testid="stMetricValue"] *,
.st-key-demo_chatcol [data-testid="stMetricValue"] * {
overflow:visible; text-overflow:clip; white-space:normal; }
.st-key-domestic_chatcol [data-baseweb="tag"], .st-key-im_chatcol [data-baseweb="tag"],
.st-key-demo_chatcol [data-baseweb="tag"] { max-width:100% !important; height:auto; }
.st-key-domestic_chatcol [data-baseweb="tag"] span,
.st-key-im_chatcol [data-baseweb="tag"] span,
.st-key-demo_chatcol [data-baseweb="tag"] span {
max-width:none !important; overflow:visible !important; text-overflow:clip !important;
white-space:normal !important; }
</style>
"""


def _requested_view() -> str:
    requested = st.query_params.get("view")
    if requested in AUDIENCES:
        return str(requested)
    default = os.environ.get("CORRIDOR_DEFAULT_VIEW", "customer")
    return default if default in AUDIENCES else "customer"


def start_run() -> str:
    """Choose the view once per browser session and apply it to this script run."""
    if st.session_state.get(SESSION_KEY) not in AUDIENCES:
        st.session_state[SESSION_KEY] = _requested_view()
    view = str(st.session_state[SESSION_KEY])
    set_audience(view)
    _run_claims.set(frozenset())
    return view


def claim_once(name: str) -> bool:
    """True the first time ``name`` is claimed in this script run, False afterwards."""
    claims = _run_claims.get()
    if name in claims:
        return False
    _run_claims.set(claims | {name})
    return True


def reapply() -> None:
    """Fragments rerun on their own thread without main(); restore locale and audience."""
    locale = st.session_state.get("locale")
    if locale:
        set_locale(locale)
    view = st.session_state.get(SESSION_KEY)
    if view not in AUDIENCES:
        view = _requested_view()
    set_audience(view)


def is_judge() -> bool:
    """Only the request-local audience decides; library calls default to the judge view."""
    return get_audience() == "judge"


def _toggle() -> None:
    view = "customer" if st.session_state.get(SESSION_KEY) == "judge" else "judge"
    st.session_state[SESSION_KEY] = view
    st.query_params["view"] = view
    set_audience(view)


def render_toggle() -> None:
    st.button(
        tr("polish.view.to_customer") if is_judge() else tr("polish.view.to_judge"),
        key="view_toggle",
        on_click=_toggle,
        use_container_width=True,
    )


def visible_pages(pages: tuple[str, ...]) -> list[str]:
    """The customer view omits evidence pages; the route tuple itself is unchanged."""
    if is_judge():
        return list(pages)
    return [page for page in pages if page not in CUSTOMER_HIDDEN_PAGES]


def page_css() -> str:
    return _BASE_CSS + ("" if is_judge() else _CUSTOMER_CSS)


def judge_strip(live_deployment: str | None = None, *, revoked: bool = False) -> None:
    """The judge view's evidence-mode line; with a live planner it names the deployment
    instead of claiming a scripted demo without model calls. A live planner that was
    switched off or revoked while an action is open is named as stopped."""
    if is_judge():
        if live_deployment and revoked:
            st.caption(tr("live.judge_strip_revoked", deployment=live_deployment))
        elif live_deployment:
            st.caption(tr("live.judge_strip", deployment=live_deployment))
        else:
            st.caption(tr("polish.view.judge_strip"))
