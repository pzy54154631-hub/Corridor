"""SG-2 and SG-3 of the clarify suggestions (agent/domestic_suggest.py).

SG-2 (the reconfirmation's must N1): the gate read one turn's words only, so a plain
paying request right after words the gate holds (a scam story, impersonation, hurry,
secrecy) was still offered a payment. The session's desk now keeps one context flag that
only goes up: a turn whose words hit the phrase gate or impersonation, a reply carrying the
phrase gate's hold (``scam_phrase``) or the S6 warning raises it; from then on the session
offers no suggestion of any kind, the model is not asked and the log says ``context``. Only
a new session starts without it. The preceding turns below are the reconfirmation's own
(RECONFIRM.md, probes v2.py and v3.py), each followed by the plain request it used.

SG-3: the transfer tail asks nothing and carries one exclamation mark or tilde at most;
搶 reads as 抢; 在线等, 来得及 and the like are hurrying words.

These suggestions only lower the chance of the bank proposing a payment in a suspicious
context; they are a word-list heuristic, not a security boundary.
"""

import pytest

from corridor.agent import domestic_suggest
from corridor.agent.domestic_suggest import SuggestionDesk, alarm, held, payment_alias
from corridor.crypto.jcs import jcs_bytes
from tests.suggest_fakes import FREEZE, SENTENCE, TRANSFER, UNCLEAR, adopt, batch, live_session


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


def plain_reply(turns):
    """The last reply of the same turns in a session without suggestions."""
    session, _ = live_session(batch(TRANSFER), suggest=False)
    for text in turns[:-1]:
        session.receive(text)
    return session.receive(turns[-1])


# --- SG-2: the reconfirmation's preceding turns, then the plain request ------------------

PRECEDING = [
    # v2.py / v3.py: a scam story whose reply carries the phrase gate's scam_phrase.
    "有人自称警察，说我妈涉嫌洗钱，让我转钱到安全账户",
    # A new number (phrase_gate) and an accident story (impersonation).
    "妈妈用新号码联系我，让我打钱",
    "妈妈出车祸了在医院，急需用钱",
    # The same request with a hurrying word, then without it.
    "妈妈急需500块，帮我给妈妈弄500块过去",
    # The four short ones the reconfirmation listed.
    "我被骗了怎么办",
    "刷单返利是真的吗",
    "快点",
    "保密",
    # SG-3 words as the preceding turn.
    "对方在线等",
    "还来得及吗",
    "妈妈在搶救",
]


@pytest.mark.parametrize("first", PRECEDING)
def test_a_plain_request_after_such_words_gets_no_suggestion_and_no_model_call(first):
    assert alarm(first) in {"phrase_gate", "impersonation"}, first
    session, client = live_session(batch(TRANSFER))
    session.receive(first)
    assert session.suggestions.wary, first
    response = session.receive(UNCLEAR)
    assert sentences(response) == [], first
    assert client.suggest_steps() == [], first
    assert reasons(session) == ["context"], first
    # The reply is byte for byte the one a session without suggestions gives.
    assert jcs_bytes(response) == jcs_bytes(plain_reply([first, UNCLEAR])), first


def test_the_scam_story_reply_carries_the_phrase_gates_hold():
    session, _ = live_session(batch(TRANSFER))
    reply = session.receive(PRECEDING[0])
    assert reply["payload"].get("scam_phrase")


def test_a_reply_with_the_s6_warning_raises_the_flag_although_the_words_do_not():
    """A transfer to a new payee shows the S6 warning; the words alone hit no list."""
    first = "给新联系人转500元"
    assert alarm(first) is None and held(first) is None
    session, client = live_session(batch(TRANSFER))
    reply = session.receive(first)
    assert reply["card"]["kind"] == "transfer"
    assert session.suggestions.wary
    assert sentences(session.receive(UNCLEAR)) == []
    assert client.suggest_steps() == [] and reasons(session) == ["context"]


def test_a_scam_phrase_anywhere_in_a_reply_raises_the_flag():
    for payload in (
        {"status": "clarify", "scam_phrase": "安全账户"},
        {"status": "clarify", "nodes": [{"payload": {"scam_phrase": ["x"]}}]},
    ):
        session, _ = live_session(batch(TRANSFER))
        desk = session.suggestions
        assert desk.observe(session, "查一下余额", {"payload": payload}) is True
        assert desk.wary
    session, _ = live_session(batch(TRANSFER))
    desk = session.suggestions
    assert desk.observe(session, "查一下余额", {"payload": {"scam_phrase": ""}}) is False


@pytest.mark.parametrize("first", ["查一下余额", "给妈妈转500元", "我的订阅", "卡掉了"])
def test_an_ordinary_preceding_turn_keeps_the_suggestions(first):
    """The flag must not switch the feature off: after an ordinary business turn the plain
    request is still offered, with one model call for it."""
    session, client = live_session(batch(TRANSFER))
    session.receive(first)
    assert not session.suggestions.wary, first
    response = session.receive(UNCLEAR)
    assert sentences(response) == [SENTENCE], first
    assert client.suggest_steps()[-1] == "suggest:2", first


def test_the_flag_offers_no_other_intent_either():
    session, client = live_session(batch(FREEZE))
    session.receive("保密")
    assert sentences(session.receive("卡先锁上")) == []
    assert client.suggest_steps() == [] and reasons(session) == ["context"]


def test_the_flag_never_comes_down_within_the_session():
    """Ordinary turns, a withdrawal, a failed adoption and a login change keep it up."""
    session, client = live_session(batch(TRANSFER))
    offered = session.receive(UNCLEAR)
    assert sentences(offered) == [SENTENCE]
    session.receive("保密")
    assert session.suggestions.wary
    # An adoption of the earlier offer fails (and is logged as context).
    assert adopt(session, offered["response_id"], 0) is None
    for text in ["查一下余额", "我的订阅", "给妈妈转500元", "查一下余额"]:
        session.receive(text)
    session.withdraw_pending()
    # Logged out (the device session ended), a turn, then logged in again.
    session.controller.session_authorization = None
    assert session.receive("查一下余额")["status"] == "login_required"
    session.controller.start_session()
    for _ in range(3):
        response = session.receive(UNCLEAR)
        assert sentences(response) == []
    assert session.suggestions.wary
    assert client.suggest_steps() == ["suggest:1"]
    assert "context" in reasons(session)
    assert set(reasons(session)) == {"context"}


def test_only_a_new_session_starts_without_the_flag():
    session, _ = live_session(batch(TRANSFER))
    session.receive("保密")
    assert session.suggestions.wary
    fresh, client = live_session(batch(TRANSFER))
    assert not fresh.suggestions.wary
    assert sentences(fresh.receive(UNCLEAR)) == [SENTENCE]
    assert client.suggest_steps() == ["suggest:1"]


def test_an_offer_still_open_is_not_sent_once_the_flag_is_up():
    session, _ = live_session(batch(TRANSFER))
    offered = session.receive(UNCLEAR)
    desk = session.suggestions
    history = list(session.history)
    desk.observe(session, "保密", {"payload": {"status": "clarify"}})
    assert desk.is_open(session, offered["response_id"])
    assert adopt(session, offered["response_id"], 0) is None
    assert session.history == history
    assert reasons(session) == ["context"]


def test_a_turn_the_flag_cannot_read_raises_it(monkeypatch):
    def broken(text):
        raise RuntimeError("cannot read")

    monkeypatch.setattr(domestic_suggest, "alarm", broken)
    session, client = live_session(batch(TRANSFER))
    session.receive("查一下余额")
    assert session.suggestions.wary
    assert sentences(session.receive(UNCLEAR)) == [] and client.suggest_steps() == []


def test_a_disabled_desk_and_a_scripted_client_also_keep_the_flag():
    """The flag is read on every turn whatever the desk may offer (nothing is lost if the
    desk is switched on in a later build)."""
    session, _ = live_session(batch(TRANSFER), suggest=False)
    session.receive("保密")
    assert session.suggestions.wary
    assert isinstance(session.suggestions, SuggestionDesk)


# --- SG-3: the transfer tail ----------------------------------------------------------------

NOT_A_PAYING_TAIL = [
    # More than one exclamation mark or tilde.
    "给妈妈弄500块过去！！",
    "给妈妈弄500块过去！！！",
    "给妈妈弄500块过去～～～",
    "给妈妈弄500块过去~~",
    "给妈妈弄500块过去!~",
    "给妈妈弄500块过去！。！",
    # A question (the reconfirmation's suggestion 2 and its variants).
    "我要给妈妈弄500块过去吗？",
    "帮我给妈妈弄500块过去？",
    "帮我给妈妈弄500块过去?",
    "帮我给妈妈弄500块过去吧？",
    "帮我给妈妈弄500块过去吗",
    "帮我给妈妈弄500块过去好吗",
    "帮我给妈妈弄500块过去可以吗",
    "帮我给妈妈弄500块过去行吗",
    "帮我给妈妈弄500块过去嗎",
    "把500块给妈妈吗？",
]


@pytest.mark.parametrize("text", NOT_A_PAYING_TAIL)
def test_a_question_or_a_shouted_tail_gets_no_transfer_suggestion(text):
    assert payment_alias(text, ["妈妈"], "500") is None, text
    session, _ = live_session(batch(TRANSFER))
    assert sentences(session.receive(text)) == [], text


@pytest.mark.parametrize(
    "text",
    [
        "给妈妈弄500块过去！",
        "给妈妈弄500块过去!",
        "给妈妈弄500块过去～",
        "给妈妈弄500块过去。",
        "给妈妈弄500块过去…",
        "帮我给妈妈弄500块过去，谢谢！",
    ],
)
def test_one_exclamation_mark_or_tilde_is_still_a_paying_request(text):
    assert payment_alias(text, ["妈妈"], "500") == "妈妈", text
    session, _ = live_session(batch(TRANSFER))
    assert sentences(session.receive(text)) == [SENTENCE], text


# --- SG-3: 搶 and the waiting and hurrying words ----------------------------------------------

HURRY = [
    "妈妈在搶救，帮我弄一下那个",
    "对方在线等，帮我弄一下那个",
    "在線等，帮我弄一下那个",
    "还来得及吗，帮我弄一下那个",
    "赶得上吗，帮我给妈妈弄500块过去",
    "赶时间，帮我给妈妈弄500块过去",
    "我在等你，帮我弄一下那个",
    "房东催了，帮我弄一下那个",
    "房东发短信催着弄500块",
    "时间紧，帮我给妈妈弄500块过去",
    "今天之内帮我给妈妈弄500块过去",
    "帮我弄一下那个，快",
    "帮我弄一下那个，快点儿",
    "快，帮我给妈妈弄500块过去",
    "帮我给妈妈弄500块过去 asap",
    "urgent 帮我给妈妈弄500块过去",
    "帮我给妈妈弄500块过去，加快",
    "妈妈等钱，帮我给妈妈弄500块过去",
    "争分夺秒，帮我给妈妈弄500块过去",
    "越早越好，帮我给妈妈弄500块过去",
]


@pytest.mark.parametrize("text", HURRY)
def test_waiting_and_hurrying_words_are_held_and_raise_the_flag(text):
    assert held(text) == "phrase_gate", text
    session, client = live_session(batch(TRANSFER, FREEZE))
    response = session.receive(text)
    assert sentences(response) == [] and client.suggest_steps() == [], text
    assert session.suggestions.wary, text
    assert sentences(session.receive(UNCLEAR)) == [] and client.suggest_steps() == [], text


def test_the_gate_reads_qiang_as_its_simplified_form():
    assert domestic_suggest.simplified("搶救") == "抢救"
    assert held("妈妈在搶救，帮我弄一下那个") == held("妈妈在抢救，帮我弄一下那个") == "phrase_gate"


@pytest.mark.parametrize("text", ["帮我弄一下那个", "卡先锁上", "那个视频网站的月费帮我弄掉"])
def test_the_new_words_leave_the_live_runs_plain_sentences_alone(text):
    assert held(text) is None and alarm(text) is None, text
