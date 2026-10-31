"""Regression tests for the fallback's re-check on the merged tree (final integration, FI-2).

The clarification-suggestion route (``/api/chat/responses/{rkey}/suggestion``) joined
``handlers.POSTS`` with the suggest line. RW-5 item 2's structural fallback covers it in
``tests/web/test_review_r6.py``, which lists it among the Write routes: a refusal or a
contained result raised after its first product write is an unknown result; raised before
any product write, it keeps its status; and every product call it makes is allow-listed or
counted. The tests here cover what the route adds: the session's suggestion desk, a
sub-object the session's proxy does not count. The web layer reads it directly (``view``,
``log.records`` and ``reason``, each on the reviewed list of ``test_web_guards.py``) and
calls ``adopt`` only through a proxy counting into the session's own ``Effects`` with no
allow list, so the desk's own writes (marking a suggestion, appending to its log) are
counted before they happen: a discard the desk decides after that (the rules no longer
read the sentence; the session carried anti-fraud context) is an unknown result and the
session is distrusted, while a refusal raised before the desk is reached (an expired
suggestion) keeps its status.
"""

import ast
import functools
import inspect
import sys
from copy import deepcopy
from pathlib import Path

import pytest

from corridor.agent import domestic_suggest
from corridor.agent.domestic_intent import IntentCandidate
from corridor.web import handlers, suggest
from corridor.web import state as web_state
from corridor.web.executor import bound
from tests.web.test_review_r5 import held
from tests.web.test_review_r6 import ROUTES, Scenario, assert_distrusted, target, web_caller
from tests.web.test_tasks import YELLOW

SUGGESTION = "/api/chat/responses/{rkey}/suggestion"
PROXY_FILE = Path(web_state.__file__).resolve()
SUGGEST_FILE = Path(suggest.__file__).resolve()
# The desk methods the web layer calls on the desk itself: reads, each on the reviewed list.
DESK_READS = {
    ("SuggestionDesk", "view"),
    ("SuggestionDesk", "reason"),
    ("SuggestionLog", "records"),
}
# The desk's one write, made only through the counting proxy.
DESK_WRITES = {("SuggestionDesk", "adopt")}


@pytest.fixture
def scenario(monkeypatch, tmp_path):
    built = Scenario(SUGGESTION, monkeypatch, tmp_path)
    try:
        yield built
    finally:
        built.close()


def rkey_of(scenario):
    return scenario.path.split("/")[4]


def desk_of(page):
    return target(page.ws.session).suggestions


def test_fi2_the_suggestion_route_is_a_write_route_of_the_fallback():
    assert SUGGESTION in handlers.POSTS and ROUTES[SUGGESTION].writes is True
    assert ROUTES[SUGGESTION].owner is suggest and ROUTES[SUGGESTION].name == "choose"


def test_fi2_the_route_calls_adopt_only_through_the_counting_proxy():
    """The source: the one ``adopt`` call is on ``_counted(ws)``, which wraps the session's
    desk in the proxy with the session's own counter and no allow list."""
    tree = ast.parse(SUGGEST_FILE.read_text(encoding="utf-8"))
    adopts = [
        ast.unparse(node.func)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "adopt"
    ]
    assert adopts == ["_counted(ws).adopt"]
    (counted,) = [
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_counted"
    ]
    (returned,) = [node for node in ast.walk(counted) if isinstance(node, ast.Return)]
    expected = "web_state.Product(ws.session.suggestions, ws.effects, frozenset())"
    assert ast.unparse(returned.value) == expected


def test_fi2_the_counted_desk_is_the_sessions_own_behind_the_sessions_counter(scenario):
    page = scenario.page
    ws, rid = page.ws, page.ws.rkeys[rkey_of(scenario)]
    counted = suggest._counted(ws)
    assert type(counted) is web_state.Product
    assert web_state.unwrap(counted) is desk_of(page)
    assert web_state.bound_effects(counted) is ws.effects
    before = ws.effects.count
    # A field read passes through uncounted; with no allow list, every method call counts.
    assert counted.wary is False and ws.effects.count == before
    assert counted.view(ws.session, rid) == desk_of(page).view(ws.session, rid)
    assert ws.effects.count == before + 1


def test_fi2_the_desks_reads_change_nothing_and_count_nothing(scenario):
    """``choices``, ``view``, the log's records, ``reason``, the judge lines and the view
    model leave the desk unchanged and the session's count where it was."""
    page = scenario.page
    ws, rkey = page.ws, rkey_of(scenario)
    rid, desk = ws.rkeys[rkey], desk_of(page)

    def fingerprint():
        return (deepcopy(desk._offers), desk.log.records(), desk.asked, desk.wary, desk.enabled)

    before, count = fingerprint(), ws.effects.count
    with bound(ws):
        items, version = suggest.choices(ws, rid)
        assert [item["state"] for item in items] == ["offered"] and version
        assert desk.view(ws.session, rid) == items
        records = desk.log.records(rid)
        assert [record["event"] for record in records] == ["suggestion_generated"]
        assert all(isinstance(desk.reason(record), str) for record in records)
        lines = suggest._judge_lines(ws, rid)
        assert len(lines) >= 3 and all(isinstance(line, str) for line in lines)
        shown = suggest.vm(ws, rid, current=True)
    assert shown is not None and shown["list_version"] == version
    assert fingerprint() == before and ws.effects.count == count


# --- the positive path: the desk counts first --------------------------------------------------


def test_fi2_the_desk_counts_before_it_checks_and_the_send_counts_again(scenario, monkeypatch):
    """Calibration: ``adopt`` is counted before the desk's check runs (the count seen at
    the check's entry is one more than before the request), and the send that follows
    counts again (``ws.session.receive``). The sentence is sent, the suggestion adopted,
    the session trusted."""
    page = scenario.page
    seen = []
    check = domestic_suggest.SuggestionDesk.check

    def recorded(self, session, response_id, index):
        seen.append(page.ws.effects.count)
        return check(self, session, response_id, index)

    monkeypatch.setattr(domestic_suggest.SuggestionDesk, "check", recorded)
    history = len(target(page.ws.session).history)
    before = page.ws.effects.count
    data = page.post(scenario.path, scenario.body)
    assert seen == [before + 1] and page.ws.effects.count > before + 1
    assert data["ok"] is True and data["rkey"] != rkey_of(scenario)
    assert len(target(page.ws.session).history) == history + 1
    assert page.ws.untrusted is False
    assert desk_of(page).log.records()[-1]["event"] == "suggestion_adopted"


class DeskCalls:
    """Every method call on the desk and its log while installed: the class, the method
    and the file of the immediate caller. Installed on the classes, so a call that bypassed
    the counting proxy is recorded too."""

    def __init__(self, monkeypatch):
        self.seen = []
        for cls in (domestic_suggest.SuggestionDesk, domestic_suggest.SuggestionLog):
            for name, member in list(vars(cls).items()):
                if name.startswith("__"):
                    continue
                if isinstance(member, staticmethod):
                    recorded = staticmethod(self._recorded(cls, name, member.__func__))
                    monkeypatch.setattr(cls, name, recorded)
                elif inspect.isfunction(member):
                    monkeypatch.setattr(cls, name, self._recorded(cls, name, member))

    def _recorded(self, cls, name, member):
        seen = self.seen

        @functools.wraps(member)
        def recorded(*args, **kwargs):
            seen.append((cls.__name__, name, sys._getframe(1).f_code.co_filename))
            return member(*args, **kwargs)

        return recorded


def test_fi2_every_desk_call_of_the_route_is_a_reviewed_read_or_counted(scenario, monkeypatch):
    """The desk-level completeness check: on the route's positive path, every desk method
    the web layer calls directly is one of the reviewed reads, and the one write
    (``adopt``) reaches the desk only through the proxy's counted wrapper."""
    page = scenario.page
    calls = DeskCalls(monkeypatch)
    data = page.post(scenario.path, scenario.body)
    assert data["ok"] is True and calls.seen
    direct = {
        (cls, name)
        for cls, name, filename in calls.seen
        if web_caller(filename) and Path(filename).resolve() != PROXY_FILE
    }
    counted = {
        (cls, name) for cls, name, filename in calls.seen if Path(filename).resolve() == PROXY_FILE
    }
    assert direct <= DESK_READS, direct
    assert counted == DESK_WRITES, counted


# --- after the desk counted: a discard is an unknown result -------------------------------------


def discard_by_recheck(page, monkeypatch):
    """The lexical router no longer reads the canonical sentence as the offered intent."""
    monkeypatch.setattr(
        domestic_suggest,
        "recognize",
        lambda text, context=None: IntentCandidate(intent="clarify", needs_clarification=True),
    )


def discard_by_context(page, monkeypatch):
    """The session's context flag (SG-2), raised as a later observation would raise it."""
    assert desk_of(page).wary is False
    monkeypatch.setattr(desk_of(page), "_wary", True)


DISCARDS = {"recheck": discard_by_recheck, "context": discard_by_context}


@pytest.mark.parametrize("reason", list(DISCARDS))
def test_fi2_a_discard_the_desk_decides_after_it_counted_is_an_unknown_result(
    scenario, monkeypatch, reason
):
    """The route reached the desk (counted), which marked the suggestion discarded and
    wrote its log, then refused. The answer is an unknown result, the session is
    distrusted and no token held before is honoured again; nothing was sent, and the list
    shows the discard with its notice."""
    page = scenario.page
    rkey, desk = rkey_of(scenario), desk_of(page)
    history = len(target(page.ws.session).history)
    DISCARDS[reason](page, monkeypatch)
    before = page.ws.effects.count
    data = page.post(scenario.path, scenario.body, expect=500)
    assert page.ws.effects.count > before
    assert_distrusted(page, data, scenario.saved, monkeypatch)
    assert len(target(page.ws.session).history) == history
    assert target(page.ws.controller).gateway.bank.execute_count == 0
    last = desk.log.records()[-1]
    assert last["event"] == "suggestion_discarded" and last["payload"]["reason"] == reason
    shown = data["state"]["chat"]["responses"][rkey]["suggestion_select"]
    assert shown["choices"][0]["state"] == "discarded" and shown["locked"] is True
    assert shown["choices"][0]["state_text"]


# --- before the desk is reached: a refusal keeps its status -------------------------------------


def test_fi2_a_refusal_before_the_desk_is_reached_keeps_its_status(scenario):
    """An expired suggestion (its reply is no longer the session's latest, made current
    again) is refused by the route before the desk is reached: no product call changed
    state, the 409 stands, the session stays trusted, the tokens held are unchanged and
    the desk logged no discard."""
    page = scenario.page
    rkey = rkey_of(scenario)
    page.send(YELLOW)
    page.post(f"/api/chat/responses/{rkey}/current", {})
    shown = page.response(rkey)["suggestion_select"]
    assert [item["state"] for item in shown["choices"]] == ["expired"]
    body = {**scenario.body, "list_version": shown["list_version"], "client_msg_id": "fi2-0002"}
    saved, history = held(page), len(target(page.ws.session).history)
    data = page.post(scenario.path, body, expect=409)
    assert data["ok"] is False and data["error"]["code"] == "not_applicable"
    assert page.ws.untrusted is False and held(page) == saved
    assert len(target(page.ws.session).history) == history
    assert [record["event"] for record in desk_of(page).log.records()] == ["suggestion_generated"]
