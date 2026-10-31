"""View models: server-rendered, plain-text, audience-trimmed projections of one session.

Read only: nothing here establishes a gate, issues a token or retires an SMS (``refresh``
does, after each POST). The functions below are the contract's entry points (§6.7).
"""

from corridor.web.vm import app, cards, chat, device, evidence, gate, phone, results, tabs, text
from corridor.web.vm.app import app_state, i18n_strings
from corridor.web.vm.evidence import technical
from corridor.web.vm.phone import payload
from corridor.web.vm.tabs import tab

__all__ = [
    "app",
    "app_state",
    "cards",
    "chat",
    "device",
    "evidence",
    "gate",
    "i18n_strings",
    "payload",
    "phone",
    "results",
    "tab",
    "tabs",
    "technical",
    "text",
]
