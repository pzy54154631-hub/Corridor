"""C3: the review of S7 round 3 (the live-model switch), one regression per must item.

1. The access field: weak codes are refused, the component answers only its own parent
   page in a secure context and names its target origin, the proof leaves the session
   once checked, and the copy no longer promises more than that.
2. Wrong codes from anonymous sessions block new grants, never an existing grant.
3. A settlement or record that fails cannot replace the call's own result or leave the
   call counted as still in flight.
4. Usage above the reservation together with a failed settlement is "to be reconciled":
   the copy says the ledger may be low, and no session reserves until an operator clears it.
5. The error a caller sees is a clean product-layer error: no cause, no context, no
   argument and no frame that holds the key.
6. The copy makes no absolute promise of equivalence and names the budget as a guard
   under assumed prices, not a provider's billing limit.
7. Each call uses the configuration read for it (a lowered cap applies at once).
8. A Windows junction or other redirection is refused for the ledger directory.

No test calls a model: transports are replaced in-process.
"""

import json
import os
import secrets
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

import pytest

from corridor.agent.llm import BudgetExceeded, LLMOutputError
from corridor.ui import live_config, live_controls, live_runtime
from corridor.ui.live_config import LiveConfig
from corridor.ui.live_runtime import JournalClient, LiveSession, SpendLedger
from corridor.ui.messages import UI_MESSAGES
from tests.unit.test_ui_live_mode import (
    FakeModel,
    Owner,
    _access_field,  # noqa: F401 - autouse: the access field is recorded, not run
    _no_inherited_client_settings,  # noqa: F401 - autouse: no inherited LLM_* settings
    call,
    environ,
    start,
    unlock,
)
from tests.unit.test_ui_live_review import no_network
from tests.unit.test_ui_live_review3 import (
    exception_text,
    reachable_text,
    schema_call,
    sentinel_dir,
)

UI = Path(live_controls.__file__).parent
STRONG = "judge-" + secrets.token_urlsafe(18)


def _session(tmp_path, code=STRONG, **extra):
    creds = sentinel_dir(tmp_path, "sk-test-" + secrets.token_hex(8), code)
    values = environ(tmp_path, **{live_config.ENV_DIR: str(creds), **extra})
    return LiveSession(LiveConfig.from_environ(values), environ=values), values


# --- 1. The access field ----------------------------------------------------------------


@pytest.mark.parametrize("code", ["4821", "judge-pass-2026", "aaaaaaaaaaaaaaaaaaaaaaaa"])
def test_a_weak_code_makes_live_access_unavailable(tmp_path, code):
    session, _ = _session(tmp_path, code=code)
    assert session.authorize(code) == "unavailable"
    assert session.snapshot()["reason"] == "weak_code"
    for locale in ("zh-CN", "en"):
        assert UI_MESSAGES["live.reason.weak_code"][locale]


def test_a_strong_access_code_is_accepted(tmp_path):
    session, _ = _session(tmp_path)
    assert session.authorize(STRONG) == "accepted"


def test_the_component_answers_only_its_parent_page_in_a_secure_context():
    html = (UI / "live_access" / "index.html").read_text(encoding="utf-8")
    script = (UI / "live_access" / "hmac.js").read_text(encoding="utf-8")
    assert 'postMessage(message, "*")' not in html
    assert "event.source !== window.parent" in html
    assert "event.origin !== window.location.origin" in html
    assert "window.isSecureContext" in html
    assert "plain HTTP" not in script and "HTTP 页面" not in script
    # Once the server has used a proof (a new challenge arrives), the field clears it.
    assert "value: null" in html


def test_the_proof_leaves_the_session_once_checked(monkeypatch, tmp_path):
    monkeypatch.setenv(live_config.ACCESS_CODE, STRONG)
    page = start(monkeypatch, tmp_path)
    unlock(page, STRONG)
    assert page.session_state["live_session"].snapshot()["authorized"] is True
    assert live_controls.PROOF_KEY not in page.session_state


def test_the_copy_promises_only_what_the_component_does():
    for key in ("live.no_secret", "live.customer.no_secret"):
        zh = UI_MESSAGES[key]["zh-CN"]
        assert "不向服务器发送明文访问码" in zh, key
        assert "都没有密钥" not in zh and "只在您的浏览器内处理" not in zh, key
    assert "所测" in UI_MESSAGES["live.no_secret"]["zh-CN"]


# --- 2. Anonymous wrong codes never end an existing grant -------------------------------


def test_anonymous_wrong_codes_block_new_grants_but_never_an_existing_one(monkeypatch, tmp_path):
    judge, values = _session(tmp_path)
    assert judge.authorize(STRONG) == "accepted"
    client = judge.client(Owner(), context_guard=None)
    for _ in range(4):
        attacker = LiveSession(LiveConfig.from_environ(values), environ=values)
        for attempt in range(5):
            attacker.authorize(f"wrong-{attempt}-" + secrets.token_hex(12))
    newcomer = LiveSession(LiveConfig.from_environ(values), environ=values)
    assert newcomer.authorize(STRONG) == "locked_all"
    snapshot = judge.snapshot()
    assert snapshot["authorized"] and snapshot["reason"] == "ready", snapshot["reason"]
    with monkeypatch.context() as patch:
        FakeModel(patch)
        assert call(client, "after-the-attack").output["intent"] == "balance_query"
    assert judge.is_current(client)


# --- 3. A failed settlement or record never replaces the call's outcome -----------------


def test_a_settlement_that_raises_keeps_the_result_and_releases_the_call(monkeypatch, tmp_path):
    session, _ = _session(tmp_path)
    assert session.authorize(STRONG) == "accepted"
    client = session.client(Owner(), context_guard=None)
    original = SpendLedger.reserve

    def reserve(self, *args, **kwargs):
        original(self, *args, **kwargs)

        def settle(amount):
            raise RuntimeError("test: the ledger driver failed unexpectedly")

        return settle

    with monkeypatch.context() as patch:
        FakeModel(patch)
        patch.setattr(SpendLedger, "reserve", reserve)
        result = call(client, "settle-raises")
    assert result.output["intent"] == "balance_query"
    assert not session.in_flight
    [outcome] = session.outcomes()
    assert outcome["settlement"] == "failed" and not outcome["failed"]


def test_a_malformed_trace_is_a_fault_record_not_a_lost_result(monkeypatch, tmp_path):
    session, _ = _session(tmp_path)
    assert session.authorize(STRONG) == "accepted"
    client = session.client(Owner(), context_guard=None)

    def broken_accounting(self, *args, **kwargs):
        raise KeyError("test: the trace could not be read")

    with monkeypatch.context() as patch:
        FakeModel(patch)
        patch.setattr(LiveSession, "_account", broken_accounting)
        result = call(client, "bad-trace")
    assert result.output["intent"] == "balance_query"
    assert not session.in_flight
    assert session.outcomes()[-1]["settlement"] == "failed"


def test_a_failing_call_with_a_failing_settlement_raises_its_own_error(monkeypatch, tmp_path):
    session, _ = _session(tmp_path)
    assert session.authorize(STRONG) == "accepted"
    client = session.client(Owner(), context_guard=None)
    original = SpendLedger.reserve

    def reserve(self, *args, **kwargs):
        original(self, *args, **kwargs)
        return lambda amount: (_ for _ in ()).throw(RuntimeError("test: settle failed"))

    with monkeypatch.context() as patch:
        FakeModel(patch, "error")
        patch.setattr(SpendLedger, "reserve", reserve)
        with pytest.raises(LLMOutputError):
            call(client, "both-fail")
    assert not session.in_flight


# --- 4. Overrun with a failed settlement is "to be reconciled" ---------------------------


def test_usage_above_the_reservation_with_a_failed_settlement_stops_every_session(
    monkeypatch, tmp_path
):
    session, values = _session(tmp_path)
    assert session.authorize(STRONG) == "accepted"
    client = session.client(Owner(), context_guard=None)
    other = LiveSession(LiveConfig.from_environ(values), environ=values)
    assert other.authorize(STRONG) == "accepted"
    other_client = other.client(Owner(), context_guard=None)
    original = SpendLedger.reserve

    def reserve(self, *args, **kwargs):
        original(self, *args, **kwargs)
        return lambda amount: (_ for _ in ()).throw(RuntimeError("test: settle failed"))

    with monkeypatch.context() as patch:
        FakeModel(patch, cost=Fraction(50))
        patch.setattr(SpendLedger, "reserve", reserve)
        call(client, "overrun")
    [outcome] = session.outcomes()
    assert outcome["overrun"] and outcome["settlement"] == "failed"
    # The ledger may now be lower than the real cost: no session reserves until an
    # operator reconciles it, whatever the remaining amount looks like.
    with monkeypatch.context() as patch:
        FakeModel(patch)
        with pytest.raises(BudgetExceeded):
            call(other_client, "after-unreconciled")
    assert other.snapshot()["reason"] == "ledger_unreconciled"
    newcomer = LiveSession(LiveConfig.from_environ(values), environ=values)
    assert newcomer.snapshot()["reason"] in {"ledger_unreconciled", "access_required"}
    zh = UI_MESSAGES["live.note.unreconciled"]["zh-CN"]
    assert "可能低估" in zh and "核对" in zh


def test_the_page_names_an_unreconciled_cost_instead_of_two_contradicting_notes(monkeypatch):
    shown = []
    monkeypatch.setattr(live_controls.st, "warning", lambda text, **_: shown.append(text))
    monkeypatch.setattr(live_controls.view_mode, "is_judge", lambda: True)
    live_controls._accounting_notes({"settlement_failed": 1, "overrun": 1, "unreconciled": 1})
    assert shown == [live_controls.tr("live.note.unreconciled")]


# --- 5. Clean errors at the product boundary ----------------------------------------------


def test_the_callers_error_holds_no_cause_argument_or_frame_with_the_key(monkeypatch, tmp_path):
    key = "sk-SENTINEL-" + secrets.token_hex(16)
    creds = sentinel_dir(tmp_path, key, STRONG)
    values = environ(tmp_path, **{live_config.ENV_DIR: str(creds)})
    session = LiveSession(LiveConfig.from_environ(values), environ=values)
    assert session.authorize(STRONG) == "accepted"
    client = session.client(Owner(), context_guard=None)

    def live(self, prompt, state, trace):
        self.api_calls += 1
        trace["source"] = "live-error"
        inner = RuntimeError("Authorization: Bearer " + self._api_key)
        raise LLMOutputError("provider echoed " + self._api_key) from inner

    with monkeypatch.context() as patch:
        patch.setattr(JournalClient, "_live", live)
        with pytest.raises(LLMOutputError) as raised:
            schema_call(client)
    error = raised.value
    assert error.__cause__ is None and error.__context__ is None
    assert not [arg for arg in error.args if key in str(arg)]
    assert key not in exception_text(error)
    assert not [text for text in reachable_text(_product_frames(error)) if key in text]
    assert not [text for text in reachable_text(session) if key in text]


def _product_frames(error):
    """The local variables of every product-code frame the error's traceback holds (the
    test's own frames hold the sentinel by construction and are skipped)."""
    frames = []
    trace = error.__traceback__
    while trace is not None:
        name = trace.tb_frame.f_code.co_filename.replace("\\", "/")
        if "/src/corridor/" in name:
            frames.append(dict(trace.tb_frame.f_locals))
        trace = trace.tb_next
    return frames


def test_a_construction_error_is_clean_too(monkeypatch, tmp_path):
    key = "sk-SENTINEL-" + secrets.token_hex(16)
    creds = sentinel_dir(tmp_path, key, STRONG)
    values = environ(tmp_path, **{live_config.ENV_DIR: str(creds)})
    session = LiveSession(LiveConfig.from_environ(values), environ=values)
    assert session.authorize(STRONG) == "accepted"

    def broken(self, *args, **kwargs):
        raise live_runtime.LLMConfigurationError("test: bad profile for " + kwargs["api_key"])

    with monkeypatch.context() as patch:
        patch.setattr(live_runtime.LLMClient, "__init__", broken)
        with pytest.raises(live_runtime.LLMConfigurationError) as raised:
            session.client(Owner(), context_guard=None)
    assert key not in exception_text(raised.value)
    assert raised.value.__cause__ is None and raised.value.__context__ is None
    assert not [text for text in reachable_text(_product_frames(raised.value)) if key in text]


# --- 6. Copy -----------------------------------------------------------------------------


def test_no_absolute_equivalence_promise_and_the_budget_is_named_a_guard():
    for locale in ("zh-CN", "en"):
        intro = UI_MESSAGES["live.intro"][locale]
        assert "逐字段相同" not in intro and "field for field" not in intro, locale
    for key in ("live.reserve", "live.caps"):
        zh = UI_MESSAGES[key]["zh-CN"]
        assert "预算守卫" in zh and "不是提供商账单的硬上限" in zh, key
    reserve = UI_MESSAGES["live.reserve"]["zh-CN"]
    for word in ("释放", "按实际", "上界", "保留", "结算失败"):
        assert word in reserve, word


# --- 7. Each call uses the configuration read for it --------------------------------------


def test_a_lowered_call_cap_applies_at_the_next_call(monkeypatch, tmp_path):
    session, values = _session(tmp_path)
    assert session.authorize(STRONG) == "accepted"
    client = session.client(Owner(), context_guard=None)
    with monkeypatch.context() as patch:
        FakeModel(patch)
        call(client, "first")
        values[live_config.SESSION_CALLS] = "1"
        with pytest.raises(BudgetExceeded):
            call(client, "second")


# --- 8. Redirected ledger directories -----------------------------------------------------


@pytest.mark.skipif(sys.platform != "win32", reason="NTFS junctions exist only on Windows")
@pytest.mark.parametrize("ancestor", [False, True])
def test_a_junction_is_refused_for_the_ledger(tmp_path, ancestor):
    target = tmp_path / "target"
    target.mkdir()
    junction = tmp_path / "junction"
    made = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(target)],
        capture_output=True,
    )
    if made.returncode != 0 or not junction.exists():
        pytest.skip("this host cannot create a directory junction")
    directory = junction / "ledger" if ancestor else junction
    with pytest.raises(live_runtime.LedgerUnavailable):
        SpendLedger(directory).remaining("CNY", Fraction(5))
    assert not (target / SpendLedger.FILE).exists()
    assert not (target / "ledger" / SpendLedger.FILE).exists()
    os.rmdir(junction)


def test_the_reservation_comment_states_the_four_byte_limit():
    source = Path(live_runtime.__file__).read_text(encoding="utf-8")
    assert "4 UTF-8 bytes" in source or "four-byte" in source
    assert json.loads(json.dumps(live_runtime.RESERVE_INPUT_TOKENS)) == 16384


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    with no_network(monkeypatch):
        yield
