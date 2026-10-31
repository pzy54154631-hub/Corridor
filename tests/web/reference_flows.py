"""The control group for tab flows (design §3.5.1): Streamlit's callbacks, called directly.

Wealth, subscriptions, the device page and Stop all live on separate Streamlit pages,
which AppTest drives at a high cost. ``ReferenceDriver`` makes the controller calls those
pages make, in their order and with their arguments: ``app::investments`` (compare, then
preview with ``app::request_id``), ``app::subscriptions`` (preview, ``approve_saved``,
cancel the chosen authorization), the R4 preset on its own controller
(``domestic_demos.prepare_demo``), ``app::device`` (``render_manual_mfa`` request and
code, then ``approve_device``) and ``app::stop_all`` (stop the bank and the advisor,
withdraw the chat envelopes, drop every ``request:*`` id but keep the sequence and the
comparison). The web side of the same flow is compared with it field by field, like the
Streamlit side of a chat flow.
"""

from corridor.agent.domestic_session import DomesticSession
from corridor.crypto.jcs import H
from corridor.sim.keys import stable_id
from corridor.ui.controller import DemoController, demo_state
from corridor.ui.wealth_controller import WealthController
from tests.web.drivers import DriverError, wrong_code
from tests.web.state_probe import observe_state


class ReferenceDriver:
    name = "reference"

    def __init__(self, *, features=()):
        self.controller = DemoController(initial_state=demo_state(showcase=True))
        self.wealth = WealthController()
        result = self.controller.start_session()
        if result.get("status") in {"accepted", "logged_in", "complete", "ready"}:
            self.wealth.start_session()
        self.session = DomesticSession(self.controller, self.wealth)
        self.requests, self.sequence = {}, 0
        self.objective = "net_return"
        self.comparison = None
        self.pending = self.result = None

    def request_id(self, kind, payload=None):
        """``app::request_id``: a rerun reuses the id, a new draft takes the next one."""
        key = f"request:{kind}:{H(payload)}"
        if key not in self.requests:
            self.requests[key] = stable_id("UI-DEMO", self.sequence, kind)
            self.sequence += 1
        return self.requests[key]

    # --- the wealth page ---------------------------------------------------------------

    def wealth_compare(self, objective):
        self.objective = objective
        self.comparison = self.controller.compare_investments(objective)

    def wealth_purchase(self, choice=None):
        if not self.comparison:
            raise DriverError("no comparison on the wealth page")
        products = self.comparison.get("candidate_ids") or []
        selection = None if choice is None else products[int(choice[1:])]
        result = self.controller.preview_investment(
            selection, request_id=self.request_id("investment", [self.objective, selection])
        )
        self.pending = result.get("pending_id")

    def preset_r4(self):
        from corridor.ui.domestic_demos import prepare_demo

        prepare_demo("suitability_r4")

    # --- the subscriptions page ----------------------------------------------------------

    def subs_query(self):
        result = self.controller.preview_subscriptions(request_id=self.request_id("subscriptions"))
        self.pending = result.get("pending_id")

    def subs_cancel(self, choice):
        refs = (self.result or {}).get("cancellable") or []
        reference = refs[int(choice[1:])]
        result = self.controller.cancel_subscription(
            reference, request_id=self.request_id("cancel-subscription", reference)
        )
        self.pending = result.get("pending_id")

    # --- the device page ---------------------------------------------------------------

    def _challenge(self):
        pending = self.controller._pending.get(self.pending)
        if not pending or not pending.get("challenge_id"):
            raise DriverError("no SMS challenge on the device page")
        return self.controller.gateway.mfa.test_code(pending["challenge_id"])

    def mfa_request(self):
        self.controller.request_mfa(self.pending)

    def mfa_verify(self):
        self.controller.complete_mfa(self.pending, code=self._challenge())

    def mfa_wrong(self):
        self.controller.complete_mfa(self.pending, code=wrong_code(self._challenge()))

    def confirm(self):
        """``approve_device`` / ``approve_saved``: a redisplay replaces the stored pending."""
        self.result = self.controller.approve_pending(self.pending)
        replacement = self.result.get("replacement") or {}
        if replacement.get("pending_id"):
            self.pending = replacement["pending_id"]

    def stop_all(self):
        self.controller.stop()
        self.session.withdraw_pending()
        self.wealth.stop()
        self.requests.clear()
        self.pending = None

    def observe(self):
        return observe_state(self.controller, self.session, None, None)

    def close(self):
        pass
