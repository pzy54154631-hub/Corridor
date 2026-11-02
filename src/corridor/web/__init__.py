"""Standalone web front end for the demo: one loopback-only tornado process.

It is another presentation and gesture entry point beside the Streamlit demo. The
controllers, the gateway and the kernel decide everything; this package renders their
state as JSON and turns explicit browser gestures into the same controller calls.
"""

SCHEMA_VERSION = 1
CONTRACT = "web-contract/1.1"

# Capabilities this build opens (contract §5.2). An ext item joins only after its own
# negative, equivalence and browser checks pass; until then its entry points answer 404.
# The ext items below joined in this order: the live-model switch,
# birthday plans with refunds, subscriptions, bills, wealth and AA participant selection.
# The authorization lab ("lab") is closed in the submission build (RW-4', RW-4''): every
# /api/lab route answers 404 and the page draws no entry. Its code and tests stay; the
# tests open it explicitly.
FEATURES: frozenset[str] = frozenset({
    "aa",
    "shortcut:transfer",
    "shortcut:cards",
    "shortcut:scam_drill",
    "live",
    "plan_select",
    "compensate",
    "shortcut:birthday",
    "tab:plans",
    "shortcut:subscriptions",
    "tab:accounts",
    "shortcut:billing",
    "tab:wealth",
})
