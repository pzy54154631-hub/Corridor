"""SG-6 of the clarify suggestions: two missed readings of the SG-2 flag.

M1: a turn that carried an external file was not read (the S6 warning a page shows for a
file is drawn from the file count, which never reached the session). Now any turn whose
page passes ``external_files`` other than 0 raises the session's context flag before the
turn is processed, so that very turn offers nothing either (a file-carrying clarification
used to be offered a payment whose adopted message then carried no file).

M2: a turn whose processing raised (for example the live intent step returning output the
client rejects) skipped the flag's reading, although its words hit the phrase gate or
impersonation. Now ``receive`` reads the words before anything else, and reads the turn
again in a ``finally`` once it was processed or failed.

These suggestions only lower the chance of the bank proposing a payment in a suspicious
context; they are a word-list heuristic, not a security boundary.
"""

import pytest

from corridor.agent import domestic_session, domestic_suggest
from corridor.agent.domestic_suggest import alarm
from corridor.agent.llm import LLMOutputError
from corridor.crypto.jcs import jcs_bytes
from tests.suggest_fakes import FREEZE, SENTENCE, TRANSFER, UNCLEAR, batch, live_session


def sentences(response):
    return [
        item["sentence"] for item in (response["payload"].get("suggestions") or {}).get("items", [])
    ]


def reasons(session):
    return [
        record["payload"]["reason"]
        for record in session.suggestions.log.records()
        if record["event"] == "suggestion_discarded"
    ]


def same_reply(left, right):
    return jcs_bytes(left) == jcs_bytes(right)


# --- M1: a turn with an external file -------------------------------------------------


def test_a_plain_request_after_a_file_carrying_transfer_gets_no_suggestion():
    session, client = live_session(batch(TRANSFER, FREEZE))
    reply = session.receive(SENTENCE, external_files=1)
    assert reply["card"]["kind"] == "transfer"
    assert session.suggestions.wary
    for text in (UNCLEAR, "帮我弄一下那个", "卡先锁上"):
        assert sentences(session.receive(text)) == [], text
    assert client.suggest_steps() == []
    assert reasons(session) == ["context", "context", "context"]


def test_a_file_carrying_clarification_offers_nothing_in_that_very_turn():
    session, client = live_session(batch(TRANSFER, FREEZE))
    reply = session.receive(UNCLEAR, external_files=1)
    assert reply["status"] == "clarify"
    assert sentences(reply) == [] and "suggestions" not in reply["payload"]
    assert client.suggest_steps() == [] and reasons(session) == ["context"]
    assert session.suggestions.wary
    # The reply is the plain clarification a session without suggestions gives.
    plain, _ = live_session(batch(TRANSFER), suggest=False)
    assert same_reply(reply, plain.receive(UNCLEAR, external_files=1))
    # Nothing was offered, so there is nothing to adopt either.
    assert session.suggestions.view(session, reply["response_id"]) == []


@pytest.mark.parametrize("count", [1, 2, 7])
def test_any_positive_file_count_raises_the_flag(count):
    session, client = live_session(batch(TRANSFER))
    session.receive("查一下余额", external_files=count)
    assert session.suggestions.wary
    assert sentences(session.receive(UNCLEAR)) == [] and client.suggest_steps() == []


@pytest.mark.parametrize("count", [-1, True, False, None, "1", "0", 1.0, 0.0])
def test_a_count_that_is_not_a_plain_zero_or_positive_int_fails_closed(count):
    session, client = live_session(batch(TRANSFER))
    session.receive("查一下余额", external_files=count)
    assert session.suggestions.wary, count
    assert sentences(session.receive(UNCLEAR)) == [] and client.suggest_steps() == []


def test_the_default_and_an_explicit_zero_keep_the_suggestions():
    for options in ({}, {"external_files": 0}):
        session, client = live_session(batch(TRANSFER))
        session.receive("查一下余额", **options)
        assert not session.suggestions.wary
        assert sentences(session.receive(UNCLEAR, **options)) == [SENTENCE]
        assert len(client.suggest_steps()) == 1


def test_a_file_on_a_turn_that_is_not_business_still_raises_the_flag():
    """Out of scope, the password refusal, a logged-out turn and even a rejected (empty)
    text are read as well (stricter than reading business turns only)."""
    for text in ("今天天气怎么样", "帮我改一下登录密码"):
        session, _ = live_session(batch(TRANSFER))
        session.receive(text, external_files=1)
        assert session.suggestions.wary, text
    session, _ = live_session(batch(TRANSFER))
    session.controller.session_authorization = None
    assert session.receive(SENTENCE, external_files=1)["status"] == "login_required"
    assert session.suggestions.wary
    session, _ = live_session(batch(TRANSFER))
    with pytest.raises(ValueError):
        session.receive("   ", external_files=1)
    assert session.suggestions.wary


def test_a_file_turn_keeps_the_flag_after_logging_in_again_and_withdrawing():
    session, client = live_session(batch(TRANSFER))
    session.receive(SENTENCE, external_files=1)
    session.withdraw_pending()
    session.controller.start_session()
    assert sentences(session.receive(UNCLEAR)) == [] and client.suggest_steps() == []
    fresh, again = live_session(batch(TRANSFER))
    assert sentences(fresh.receive(UNCLEAR)) == [SENTENCE] and len(again.suggest_steps()) == 1


def test_the_file_count_is_read_before_the_turn_is_processed(monkeypatch):
    seen = []
    session, _ = live_session(batch(TRANSFER))
    classify = session._classify

    def recorded(text):
        seen.append(session.suggestions.wary)
        return classify(text)

    monkeypatch.setattr(session, "_classify", recorded)
    session.receive(SENTENCE, external_files=1)
    assert seen == [True]


def test_the_file_count_never_reaches_a_prompt_or_the_request():
    session, client = live_session(batch(TRANSFER))
    session.receive(SENTENCE, external_files=1)
    assert "external" not in repr(client.prompts) + repr(client.tool_states)
    assert "external" not in repr(session.history)


# --- M2: a turn whose processing raised -------------------------------------------------

RAISING = [
    "妈妈急需500块，给妈妈转500元",  # impersonation
    "保密，给妈妈转500元",  # phrase_gate
    "快点给妈妈转500元",  # phrase_gate
]


@pytest.mark.parametrize("words", RAISING)
def test_held_words_whose_turn_raised_still_raise_the_flag(words):
    assert alarm(words) in {"phrase_gate", "impersonation"}
    session, client = live_session(batch(TRANSFER), intent=LLMOutputError("bad output"))
    with pytest.raises(LLMOutputError):
        session.receive(words)
    assert session.suggestions.wary, words
    client.intent = None
    for text in (UNCLEAR, "帮我弄一下那个", UNCLEAR):
        assert sentences(session.receive(text)) == [], (words, text)
    assert client.suggest_steps() == [], words
    assert reasons(session) == ["context", "context", "context"]


def test_the_words_are_read_before_the_turn_is_processed(monkeypatch):
    seen = []
    session, _ = live_session(batch(TRANSFER))
    classify = session._classify

    def recorded(text):
        seen.append(session.suggestions.wary)
        return classify(text)

    monkeypatch.setattr(session, "_classify", recorded)
    session.receive(RAISING[1])
    assert seen == [True]


def test_a_turn_that_raises_before_the_intent_step_is_read_too(monkeypatch):
    def broken(text, context):
        raise RuntimeError("router failed")

    monkeypatch.setattr(domestic_session, "recognize", broken)
    session, client = live_session(batch(TRANSFER))
    with pytest.raises(RuntimeError):
        session.receive(RAISING[2])
    assert session.suggestions.wary
    monkeypatch.undo()
    assert sentences(session.receive(UNCLEAR)) == [] and client.suggest_steps() == []


def test_the_turn_is_read_again_after_its_processing_raised(monkeypatch):
    """The ``finally`` reading runs on the exception path (without a reply)."""
    calls = []
    original = domestic_suggest.observe

    def recorded(session, text, response=None, **options):
        calls.append((text, response, options))
        return original(session, text, response, **options)

    monkeypatch.setattr(domestic_suggest, "observe", recorded)
    session, _ = live_session(batch(TRANSFER), intent=LLMOutputError("bad output"))
    with pytest.raises(LLMOutputError):
        session.receive(SENTENCE)
    assert calls == [(SENTENCE, None, {"external_files": 0}), (SENTENCE, None, {})]


def test_an_ordinary_turn_that_raised_keeps_the_suggestions():
    """No over-blocking: a failed turn whose words hit no list leaves the flag down."""
    session, client = live_session(batch(TRANSFER), intent=LLMOutputError("bad output"))
    with pytest.raises(LLMOutputError):
        session.receive(SENTENCE)
    assert not session.suggestions.wary
    client.intent = None
    assert sentences(session.receive(UNCLEAR)) == [SENTENCE]
    assert len(client.suggest_steps()) == 1


def test_a_desk_read_without_a_reply_reads_the_words_and_the_count_only():
    session, _ = live_session(batch(TRANSFER))
    desk = session.suggestions
    assert desk.observe(session, "查一下余额") is False
    assert desk.observe(session, "查一下余额", None, external_files=0) is False
    assert desk.observe(session, "保密") is True
    session, _ = live_session(batch(TRANSFER))
    assert session.suggestions.observe(session, None, None, external_files=1) is True
