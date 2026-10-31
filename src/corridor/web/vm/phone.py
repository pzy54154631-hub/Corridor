"""The simulated phone page's data: read only from the process outbox, never a session.

Only this payload can carry a verification code (INV-2.4), and only for a pairing the
caller found in its allow list; an unknown or revoked pairing gets the same unpaired
line and no messages. The caller has set the locale and the audience.
"""

from typing import Any

from corridor.web.catalog import wtr
from corridor.web.compat import OUTBOX, H, get_audience, sms_text, tr
from corridor.web.vm.text import plain

RETIRED = {"used": "polish.phone.used", "expired": "polish.phone.expired"}


def payload(pair: str, *, allowed: bool, public: bool = False) -> dict[str, Any]:
    """The phone page's data; ``public`` adds the demo notice (public mode only)."""
    paired = allowed and OUTBOX.known(pair)
    inbox = OUTBOX.inbox(pair) if paired else ()
    latest = next((sms for sms in inbox if sms.status == "active"), None)
    ordered = ([latest] if latest is not None else []) + [sms for sms in inbox if sms is not latest]
    messages = []
    for sms in ordered:
        message = {"text": plain(sms_text(sms)), "status": sms.status, "latest": sms is latest}
        if sms.status in RETIRED:
            # The masked code alone does not say why it is gone (W13, web-contract/1.1).
            message["status_text"] = plain(tr(RETIRED[sms.status]))
        messages.append(message)
    shown: dict[str, Any] = {
        "ok": True,
        # Changes whenever a copy arrives, is used or expires; never a full digest.
        "version": H([[sms.ref, sms.status, bool(sms.code)] for sms in ordered])[:16],
        "title": plain(tr("polish.phone.title")),
        "status_bar": plain(tr("polish.phone.status_bar")),
        "corner": plain(tr("simulation_notice")),
        "messages": messages,
        "empty": plain(tr("polish.phone.empty")) if paired and not messages else None,
        "unpaired": None if paired else plain(tr("polish.phone.unpaired")),
        "footer": plain(tr("polish.phone.footer")),
    }
    if get_audience() == "judge":
        shown["disclosure"] = plain(tr("polish.phone.disclosure"))
    if public:
        shown["banner"] = plain(wtr("web.vm.public.phone_banner"))
        shown["public_disclosure"] = plain(wtr("web.vm.public.disclosure"))
        shown["public_details_title"] = plain(wtr("web.vm.public.details_title"))
    return shown
