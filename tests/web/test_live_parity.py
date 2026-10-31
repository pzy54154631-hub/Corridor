"""S7 on the web: the live panel says what the Streamlit page says (design §3.5.3).

Every condition is reached twice from the same gestures, each pass with its own process
variables, journal directory and patches: on a Streamlit page whose ``st`` is a recorder
(``render_live_controls`` over a session-state dict, one page run after each gesture),
and on a web session through the adapter (``sync`` around business and live POSTs, then
``view``). The lines shown, the switch's flags, the dropdown, the judge strip, the corner
notice and the page-level failure lines must be equal, in the customer and in the judge
view. The transport is the test-only fake model; nothing leaves the machine.
"""

import html
import re
import secrets
import socket
import sqlite3
from collections import Counter
from contextlib import contextmanager

import pytest

from corridor.agent.domestic_session import DomesticSession
from corridor.agent.llm import _ENV_FIELDS
from corridor.channels.im import IMAdapter
from corridor.i18n import reset_locale, set_locale
from corridor.ui import app, live_config, live_controls, live_runtime, view_mode
from corridor.ui.audience import reset_audience, set_audience
from corridor.ui.controller import DemoController, demo_state
from corridor.ui.live_runtime import LiveSession, SpendLedger, access_proof
from corridor.ui.messages import tr
from corridor.ui.wealth_controller import WealthController
from corridor.web import live
from corridor.web.catalog import wtr
from corridor.web.state import new_web_session
from tests.unit.test_ui_live_mode import ACCESS_CODE, FakeModel, credential_dir, environ

FAILURES = (ValueError, RuntimeError, KeyError, TimeoutError, OSError)
GENERIC = "The operation was not completed. Review current facts and retry from a fresh proposal."
LOGGED_IN = {"accepted", "logged_in", "complete", "ready"}
LOOPBACK = {"127.0.0.1", "::1", "localhost"}
BADGE = re.compile(r'class="live-badge live-badge-(?:demo|model)"[^>]*>(.*)</div>', re.S)
CORNER = re.compile(r">([^<]*)</div>\s*$")


def _offline(patch):
    for name in (*_ENV_FIELDS, *live_config.PROCESS_NAMES):
        patch.delenv(name, raising=False)
    connect = socket.socket.connect

    def loopback_only(self, address):
        if not (isinstance(address, tuple) and address[0] in LOOPBACK):
            raise OSError("test: network disabled")
        return connect(self, address)

    patch.setattr(socket.socket, "connect", loopback_only)


class Recorder:
    """Stands in for ``st`` in ``ui/live_controls``: keeps what one page run draws."""

    def __init__(self, state):
        self.session_state = state
        self.lines = []
        self.flags = {}

    def _add(self, kind, text):
        self.lines.append((kind, str(text)))

    def markdown(self, body, **kwargs):
        found = BADGE.search(str(body))
        if found:
            self._add("badge", html.unescape(found.group(1)))
        else:
            self._add("markdown", body)

    def caption(self, text):
        self._add("caption", text)

    def error(self, text):
        self._add("error", text)

    def warning(self, text):
        self._add("warning", text)

    def success(self, text):
        self._add("success", text)

    @contextmanager
    def expander(self, title, expanded=False):
        self._add("expander", title)
        yield

    @contextmanager
    def container(self):
        yield

    def empty(self):
        return self

    def selectbox(self, label, options, index=0, format_func=str, key=None, disabled=False):
        self.flags["deployments"] = [format_func(option) for option in options]
        self.flags["selected"] = options[index]
        self.flags["select_disabled"] = bool(disabled)
        return self.session_state.get(key, options[index])

    def toggle(self, label, key=None, disabled=False):
        self._add("label", label)
        self.flags["toggle"] = (bool(self.session_state.get(key, False)), bool(disabled))
        return bool(self.session_state.get(key, False))

    def access(self, *, nonce, label, button, disabled, key):
        self._add("label", label)
        self._add("label", button)
        self.flags["unlock_disabled"] = bool(disabled)


def _logged_in(controller, wealth):
    if controller.start_session().get("status") in LOGGED_IN:
        wealth.start_session()


class StreamlitSide:
    """The Streamlit page: one run of the live controls after each gesture."""

    def __init__(self, patch, view):
        self.patch = patch
        self.controller = DemoController(initial_state=demo_state(showcase=True))
        wealth = WealthController()
        _logged_in(self.controller, wealth)
        self.session = DomesticSession(self.controller, wealth)
        self.state = {"domestic_session": self.session}
        self.page = None
        self.page_failure = []

    def render(self):
        page = Recorder(self.state)
        self.patch.setattr(live_controls, "st", page)
        self.patch.setattr(live_controls, "_access_component", page.access)
        live_controls.render_live_controls(self.controller)
        self.page = page

    def open(self):
        self.render()

    def rerun(self):
        self.render()

    def unlock(self, code=ACCESS_CODE):
        nonce = self.state["live_session"].challenge()
        self.state[live_controls.PROOF_KEY] = {"nonce": nonce, "proof": access_proof(code, nonce)}
        self.render()

    def toggle(self, on):
        self.state[live_controls.TOGGLE_KEY] = on
        self.render()

    def send(self, text):
        # submit_request in the page body; a failure is caught by app.main, which names
        # the live part of it for this run's calls only.
        mark = live_controls.operation_mark(self.controller)
        try:
            response = self.session.receive(text)
        except FAILURES as error:
            failed = Recorder(self.state)
            self.patch.setattr(live_controls, "st", failed)
            failed.error(tr(GENERIC))
            live_controls._run_mark.set(mark)
            live_controls.render_live_failure(self.controller, error)
            # Its judge counters are redrawn in their own slot, compared with the panel.
            self.page_failure = [
                line for kind, line in failed.lines if kind in {"error", "warning"}
            ]
        else:
            live_controls.remember("domestic", response["response_id"], mark)
        self.render()

    def confirm(self):
        """``confirm_response`` on the latest reply, then the page runs."""
        rid = self.session.history[-1]["response_id"]
        mark = live_controls.operation_mark(self.controller)
        try:
            IMAdapter(self.session).click("confirm", rid)
        except FAILURES as error:
            live_controls.remember_failure("domestic", rid, mark, error)
        else:
            live_controls.remember("domestic", rid, mark)
        self.render()

    def notes(self):
        page = Recorder(self.state)
        self.patch.setattr(live_controls, "st", page)
        response = self.session.response(self.session.history[-1]["response_id"])
        live_controls.render_response_notes(self.controller, response, "domestic")
        return [("info" if kind == "caption" else kind, line) for kind, line in page.lines]

    def failure(self):
        recorded = live_controls.recorded_failure(
            "domestic", self.session.history[-1]["response_id"]
        )
        page = Recorder(self.state)
        self.patch.setattr(live_controls, "st", page)
        live_controls.render_recorded_failure(recorded)
        return [line for kind, line in page.lines], recorded["source"]

    def shown(self):
        page = self.page
        lines = [line for kind, line in page.lines if kind != "markdown"]
        lines.extend(page.flags.get("deployments", []))
        label = live_controls.live_label(self.controller)
        revoked = live_controls.live_revoked(self.controller)
        strip = Recorder(self.state)
        self.patch.setattr(view_mode, "st", strip)
        view_mode.judge_strip(label, revoked=revoked)
        corner = Recorder(self.state)
        app.persistent_notice(corner, label if label and not revoked else None)
        flags = {name: page.flags.get(name) for name in ("toggle", "unlock_disabled")}
        flags["select_disabled"] = page.flags.get("select_disabled")
        flags["selected"] = page.flags.get("selected")
        shell = {
            "strip": strip.lines[0][1] if strip.lines else None,
            "corner": html.unescape(CORNER.search(corner.lines[0][1]).group(1)),
            "page_failure": self.page_failure,
            "toggle": bool(self.state.get(live_controls.TOGGLE_KEY)),
            "client": type(self.controller.client).__name__,
        }
        return Counter(lines), flags, shell


class WebSide:
    """A web session: POSTs through the adapter as the handlers call it (design §2.3)."""

    def __init__(self, patch, view):
        self.ws = new_web_session(
            view, "zh-CN", browser_id=secrets.token_urlsafe(32), tab_id=secrets.token_urlsafe(24)
        )
        live.on_new(self.ws)
        _logged_in(self.ws.controller, self.ws.wealth)

    def _business(self, action):
        ws = self.ws
        ws.revision += 1
        live.sync(ws, apply=True)
        try:
            return action()
        except Exception as error:
            assert getattr(error, "code", None) == "operation_failed"
        finally:
            live.sync(ws, apply=True)

    def _live(self, action):
        self.ws.revision += 1
        result = action()
        live.sync(self.ws, apply=True)
        return result

    def open(self):
        pass

    def rerun(self):
        self._business(lambda: None)

    def unlock(self, code=ACCESS_CODE):
        ws = self.ws
        nonce = self._live(lambda: live.challenge(ws))
        self._live(lambda: live.unlock(ws, nonce, access_proof(code, nonce)))

    def toggle(self, on):
        self._live(lambda: live.toggle(self.ws, on))

    def send(self, text):
        ws = self.ws

        def action():
            with live.operation(ws, "domestic") as op:
                reply = ws.session.receive(text)
                op.bind(reply["response_id"])
                ws.rkey_of.setdefault(reply["response_id"], f"R{len(ws.rkey_of) + 1}")

        self._business(action)

    def confirm(self):
        ws = self.ws
        rid = ws.session.history[-1]["response_id"]

        def action():
            with live.operation(ws, "domestic", rid):
                IMAdapter(ws.session).click("confirm", rid)

        self._business(action)

    def notes(self):
        ws = self.ws
        response = ws.session.response(ws.session.history[-1]["response_id"])
        return [(note["level"], note["text"]) for note in live.notes(ws, response, "domestic")]

    def failure(self):
        ws = self.ws
        rid = ws.session.history[-1]["response_id"]
        block = live.failure_vm(ws, "domestic", rid)
        return block["lines"] + [block["cancel_hint"]], ws.failures[("domestic", rid)]["source"]

    def shown(self):
        ws = self.ws
        panel = live.view(ws)
        lines = []
        if panel["enabled"]:
            lines += [panel["title"], panel["intro"], panel["unlock"]["label"]]
            lines += [panel["unlock"]["button"], panel["toggle"]["label"]]
            lines += [
                item["label"]
                + (" · " + item["credential_text"] if "credential_text" in item else "")
                for item in panel["deployments"]
            ]
            lines += [panel.get("price"), panel["unlock"]["result_text"], panel["revoked_text"]]
            lines += [panel["reason_text"], panel["boundary_text"], panel["caps_text"]]
            lines += [panel["kernel_note"], panel["no_secret"]]
        else:
            lines += [panel["off_text"], panel["reason_text"]]
        lines += [panel["badge"]["text"], panel["notice"], *panel.get("counters", [])]
        enabled = panel["enabled"]
        flags = {
            "toggle": (panel["toggle"]["on"], panel["toggle"]["disabled"]) if enabled else None,
            "unlock_disabled": panel["unlock"]["disabled"] if enabled else None,
            "select_disabled": (not panel["boundary"] or panel["badge"]["kind"] == "model")
            if enabled
            else None,
            "selected": panel["selected"],
        }
        notices = [item["text"] for item in live.page_failure(ws)]
        shell = {
            "strip": live.judge_strip(ws),
            "corner": live.corner(ws),
            "page_failure": [text for text in notices if text != panel["notice"]],
            "toggle": ws.live_toggle,
            "client": type(ws.controller.client).__name__,
        }
        return Counter(line for line in lines if line is not None), flags, shell


# --- the conditions of design §3.5.3 ---------------------------------------------------------


def _hosted(patch, root, **extra):
    for name, value in environ(root, **extra).items():
        patch.setenv(name, value)
    return root / "journal"


def off(side, patch, root):
    patch.setenv(live_config.ENV_DIR, str(credential_dir(root)))
    side.open()


def invalid_scripted(side, patch, root):
    _hosted(patch, root, **{live_config.SESSION_CALLS: "two"})
    side.open()


def invalid_live(side, patch, root):
    _hosted(patch, root)
    side.open()
    side.unlock()
    side.toggle(True)
    side.send("给妈妈转800元")
    patch.setenv(live_config.SESSION_CALLS, "two")
    side.rerun()


def inherited_env(side, patch, root):
    _hosted(patch, root)
    patch.setenv("LLM_MAX_USD", "100")
    side.open()


def missing_journal(side, patch, root):
    _hosted(patch, root)
    patch.delenv(live_config.JOURNAL_DIR)
    side.open()


def missing_credentials(side, patch, root):
    _hosted(patch, root)
    empty = root / "empty-creds"
    empty.mkdir()
    patch.setenv(live_config.ENV_DIR, str(empty))
    side.open()


def access_required(side, patch, root):
    _hosted(patch, root)
    side.open()


def unlocked_and_on(side, patch, root):
    _hosted(patch, root)
    side.open()
    side.unlock()
    side.toggle(True)
    side.send("查一下余额")


def locked(side, patch, root):
    _hosted(patch, root)
    side.open()
    for _ in range(live_runtime.LOCK_AFTER_FAILURES):
        side.unlock("nope")
    side.unlock()


def locked_all(side, patch, root):
    ledger = SpendLedger(_hosted(patch, root))
    for _ in range(live_runtime.GLOBAL_LOCK_AFTER):
        ledger.record_access_failure()
    side.open()


def ledger_unavailable(side, patch, root):
    _hosted(patch, root)
    side.open()
    side.unlock()
    side.toggle(True)

    def broken(self):
        raise sqlite3.OperationalError("test: database is locked")

    patch.setattr(SpendLedger, "_connection", broken)
    side.rerun()


def revoked_by_rotation(side, patch, root):
    _hosted(patch, root)
    side.open()
    side.unlock()
    side.toggle(True)
    patch.setenv(live_config.ACCESS_CODE, "rotated-code")
    side.rerun()


def cap_at_boundary(side, patch, root):
    _hosted(patch, root, **{live_config.SESSION_CALLS: "1"})
    side.open()
    side.unlock()
    side.toggle(True)
    side.send("查一下余额")


def cap_mid_transaction(side, patch, root):
    _hosted(patch, root, **{live_config.SESSION_CALLS: "2"})
    side.open()
    side.unlock()
    side.toggle(True)
    side.send("给妈妈转800元")


def stopped_mid_transaction(side, patch, root):
    _hosted(patch, root)
    side.open()
    side.unlock()
    side.toggle(True)
    side.send("给妈妈转800元")
    patch.setenv(live_config.SWITCH, "0")
    side.rerun()


def _unbuildable(patch):
    def refuse(self, item):
        raise OSError("test: journal directory not writable")

    patch.setattr(LiveSession, "_journal_dir", refuse)


def rebuild_failed_scripted(side, patch, root):
    _hosted(patch, root)
    side.open()
    side.unlock()
    _unbuildable(patch)
    side.toggle(True)


def rebuild_failed_live(side, patch, root):
    _hosted(patch, root)
    side.open()
    side.unlock()
    side.toggle(True)
    _unbuildable(patch)
    # A changed cap drops the client; it must be built again at the next boundary.
    patch.setenv(live_config.BUDGET_USD, "4")
    side.rerun()


def failed_at_confirm(side, patch, root):
    _hosted(patch, root)
    side.open()
    side.unlock()
    side.toggle(True)
    side.send("给妈妈转800元")
    patch.setenv(live_config.SWITCH, "0")
    side.confirm()


def failed_before_a_reply(side, patch, root):
    _hosted(patch, root)
    side.open()
    side.unlock()
    side.toggle(True)
    FakeModel(patch, behaviour="bad")
    side.send("给妈妈转800元")


CONDITIONS = [
    off,
    invalid_scripted,
    invalid_live,
    inherited_env,
    missing_journal,
    missing_credentials,
    access_required,
    unlocked_and_on,
    locked,
    locked_all,
    ledger_unavailable,
    revoked_by_rotation,
    cap_at_boundary,
    cap_mid_transaction,
    stopped_mid_transaction,
    rebuild_failed_scripted,
    rebuild_failed_live,
    failed_at_confirm,
    failed_before_a_reply,
]


@pytest.fixture(params=["customer", "judge"])
def view(request):
    audience, locale = set_audience(request.param), set_locale("zh-CN")
    yield request.param
    reset_locale(locale)
    reset_audience(audience)


def run(kind, view, root, scenario, *, behaviour="ok"):
    """One isolated pass: its own variables, journal directory, patches and fake model."""
    root.mkdir(parents=True)
    try:
        with pytest.MonkeyPatch.context() as patch:
            _offline(patch)
            FakeModel(patch, behaviour=behaviour)
            side = kind(patch, view)
            return scenario(side, patch, root)
    finally:
        # The page's per-run placeholders must not outlive this pass.
        live_controls._judge_slot.set(None)
        live_controls._run_mark.set(None)


@pytest.mark.parametrize("condition", CONDITIONS, ids=[item.__name__ for item in CONDITIONS])
def test_the_live_panel_matches_the_streamlit_page(condition, view, tmp_path):
    def scenario(side, patch, root):
        condition(side, patch, root)
        return side.shown()

    expected = run(StreamlitSide, view, tmp_path / "streamlit", scenario)
    shown = run(WebSide, view, tmp_path / "web", scenario)
    if condition is inherited_env and view == "judge":
        # P-DIFF-28 (R1-E15): the web's judge view counts the inherited variables and
        # does not name them; every other line is the page's own.
        named = tr("live.reason.inherited_env", names="LLM_MAX_USD")
        counted = wtr("web.vm.live.inherited_env", count=1)
        assert named in expected[0] and counted in shown[0]
        assert not any("LLM_MAX_USD" in str(line) for line in shown[0])
        lines = Counter({
            (counted if line == named else line): n for line, n in expected[0].items()
        })
        expected = (lines, *expected[1:])
    assert shown[0] == expected[0]
    assert shown[2] == expected[2]
    for name in ("toggle", "unlock_disabled", "select_disabled"):
        assert shown[1][name] == expected[1][name], name
    if expected[1]["selected"] is not None:
        assert shown[1]["selected"] == expected[1]["selected"]


def _settlement_refused(patch):
    original = SpendLedger.reserve

    def reserve(self, *args, **kwargs):
        original(self, *args, **kwargs)

        def settle(actual):
            raise live_runtime.LedgerUnavailable("test: settlement refused")

        return settle

    patch.setattr(SpendLedger, "reserve", reserve)


@pytest.mark.parametrize(
    ("behaviour", "refused"),
    [("timeout", False), ("ok", True), ("ok", False)],
    ids=["partly_scripted", "settlement_failed", "recorded"],
)
def test_reply_notes_and_failure_blocks_match(behaviour, refused, view, tmp_path):
    def scenario(side, patch, root):
        if refused:
            _settlement_refused(patch)
        _hosted(patch, root)
        side.open()
        side.unlock()
        side.toggle(True)
        side.send("给妈妈转800元")
        notes = side.notes()
        # The confirm then fails on the switched-off client: the same failure block.
        patch.setenv(live_config.SWITCH, "0")
        side.confirm()
        return notes, side.failure()

    expected = run(StreamlitSide, view, tmp_path / "streamlit", scenario, behaviour=behaviour)
    shown = run(WebSide, view, tmp_path / "web", scenario, behaviour=behaviour)
    assert shown == expected
    assert expected[1][1] == "live-revoked"
    if behaviour == "timeout":
        assert expected[0][0][0] == "warning"


def _changed_handle(step, output):
    """The model echoes the plan with a forged signed handle; the planner refuses it."""
    if step.startswith("plan:"):
        output = dict(output)
        output["amount"] = {**output["amount"], "ref": "INPUT-FORGED"}
    return output


def test_a_refused_plan_with_a_failed_settlement_gives_the_same_block(view, tmp_path):
    def scenario(side, patch, root):
        _hosted(patch, root)
        side.open()
        side.unlock()
        side.toggle(True)
        side.send("给妈妈转800元")
        FakeModel(patch, rule=_changed_handle)
        _settlement_refused(patch)
        side.confirm()
        return side.failure(), side.shown()

    expected = run(StreamlitSide, view, tmp_path / "streamlit", scenario)
    shown = run(WebSide, view, tmp_path / "web", scenario)
    assert shown[0] == expected[0]
    assert shown[1][0] == expected[1][0]
    lines, source = expected[0]
    assert source == "agent-rejected"
    assert (tr("live.note.settlement_failed") in lines) is (view == "judge")


@pytest.mark.parametrize("view", ["customer", "judge"])
@pytest.mark.parametrize(
    "condition",
    [unlocked_and_on, locked, cap_at_boundary, invalid_live, failed_before_a_reply],
    ids=lambda item: item.__name__,
)
def test_the_english_panel_matches_too(condition, view, tmp_path):
    audience, locale = set_audience(view), set_locale("en")
    try:

        def scenario(side, patch, root):
            condition(side, patch, root)
            return side.shown()

        expected = run(StreamlitSide, view, tmp_path / "streamlit", scenario)
        shown = run(WebSide, view, tmp_path / "web", scenario)
    finally:
        reset_locale(locale)
        reset_audience(audience)
    assert shown[0] == expected[0]
    assert shown[2] == expected[2]
    assert all(line.isascii() or "·" in line for line in shown[0])
