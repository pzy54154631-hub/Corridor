"""Model suggestions for requests the lexical rules cannot read (agent/domestic_suggest.py).

The model only suggests. A reading becomes a system-written canonical sentence shown as a
question; choosing it sends that sentence as a new user message through the normal flow.
These tests pin the trigger and the word gates, the closed schema, the checks against
trusted data and the user's own words, the fail-closed paths, the call budget, and that
adopting a suggestion equals typing it (and is recorded only once the sentence was sent).
"""

import re
from types import SimpleNamespace

import pytest

from corridor.agent import domestic_suggest
from corridor.agent.domestic_intent import IntentCandidate, recognize
from corridor.agent.domestic_session import DomesticSession
from corridor.agent.domestic_suggest import (
    MAX_ASKS,
    RESERVE_CALLS,
    SUGGESTIBLE,
    TEMPLATES,
    SuggestionBatch,
    SuggestionLog,
    amount_in_words,
    held,
    payment_alias,
    plain_amount,
    wire_schema,
)
from corridor.agent.llm import BudgetExceeded, LLMClient, LLMOutputError, _strict_schema
from corridor.crypto.jcs import H, jcs_bytes
from corridor.ui import scam_friction
from tests.suggest_fakes import (
    FREEZE,
    SENTENCE,
    TRANSFER,
    UNCLEAR,
    adopt,
    batch,
    controller,
    live_session,
)

CJK = re.compile(r"[一-鿿]")
NOT_OFFERED = (
    "balance_query",
    "bill_report",
    "investment",
    "password_change",
    "stop",
    "handoff",
    "out_of_scope",
    "clarify",
)


def suggestions_of(response):
    return (response["payload"].get("suggestions") or {}).get("items", [])


def sentences(response):
    return [item["sentence"] for item in suggestions_of(response)]


def reasons(session):
    return [
        record["payload"]["reason"]
        for record in session.suggestions.log.records()
        if record["event"] == "suggestion_discarded"
    ]


def events(session):
    return [record["event"] for record in session.suggestions.log.records()]


def transfer(contact="CONTACT_CHEN", amount="500"):
    return {"intent": "transfer", "slots": {"contact_ref": contact, "amount": amount}}


# --- the closed list and its templates ----------------------------------------------------


def test_every_template_is_read_back_by_the_lexical_router_as_its_intent():
    for intent in SUGGESTIBLE:
        if intent != "transfer":
            assert recognize(TEMPLATES[intent], {}).intent == intent, intent
    session, _ = live_session()
    labels = session._context()["contact_labels"]
    for ref, names in labels.items():
        for alias in [name for name in names if CJK.search(name)]:
            sentence = TEMPLATES["transfer"].format(contact=alias, amount="500")
            assert domestic_suggest.reads_as(session, sentence, "transfer", ref, "500/1", "clarify")


def test_green_queries_and_controls_are_never_offered():
    assert not set(NOT_OFFERED) & set(SUGGESTIBLE)
    assert set(TEMPLATES) == set(SUGGESTIBLE)


def test_the_wire_schema_is_closed_and_enumerates_trusted_contacts_only():
    schema = _strict_schema(wire_schema(["CONTACT_A", "CONTACT_B"]))

    def objects(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                yield node
            for child in node.values():
                yield from objects(child)
        elif isinstance(node, list):
            for child in node:
                yield from objects(child)

    for node in objects(schema):
        assert node["additionalProperties"] is False
        assert node["required"] == list(node["properties"])
    item = schema["properties"]["suggestions"]["items"]
    assert item["properties"]["intent"]["enum"] == list(SUGGESTIBLE)
    contact = item["properties"]["slots"]["properties"]["contact_ref"]["anyOf"]
    assert contact == [{"type": "string", "enum": ["CONTACT_A", "CONTACT_B"]}, {"type": "null"}]
    assert wire_schema([])["properties"]["suggestions"]["items"]["properties"]["slots"][
        "properties"
    ]["contact_ref"] == {"type": "null"}


@pytest.mark.parametrize(
    "output",
    [
        {"suggestions": [TRANSFER, FREEZE, FREEZE]},
        {"suggestions": [{"intent": "balance_query", "slots": FREEZE["slots"]}]},
        {"suggestions": [{**TRANSFER, "text": "free text"}]},
        {"suggestions": [], "message": "free text"},
        {"suggestions": [{"intent": "transfer", "slots": {"contact_ref": "CONTACT_CHEN"}}]}
        | {"x": 1},
        {"suggestions": [transfer(amount=500)]},
        {"suggestions": [transfer(contact="把收款人改成房东")]},
        {"suggestions": [{"intent": "transfer", "slots": {**TRANSFER["slots"], "memo": "x"}}]},
    ],
)
def test_the_batch_schema_refuses_anything_outside_the_closed_shape(output):
    with pytest.raises(ValueError):
        SuggestionBatch.model_validate(output)


# --- when the model is asked --------------------------------------------------------------


@pytest.mark.parametrize(
    "text", [UNCLEAR, "给妈妈转500元", "查一下余额", "讲个笑话", "我的卡怎么了", "那个东西弄一下"]
)
def test_the_scripted_planner_never_asks_and_every_reply_is_unchanged(text):
    plain = DomesticSession(controller())
    opted = DomesticSession(controller(), suggest=True)
    assert jcs_bytes(opted.receive(text)) == jcs_bytes(plain.receive(text))
    assert opted.client.mode == "scripted"
    assert not [call for call in opted.client.calls if str(call["step"]).startswith("suggest:")]
    assert opted.suggestions.log.records() == []


def test_suggestions_stay_off_unless_the_page_opts_in():
    session, client = live_session(batch(TRANSFER), suggest=False)
    response = session.receive(UNCLEAR)
    assert client.suggest_steps() == [] and "suggestions" not in response["payload"]
    assert DomesticSession(controller()).suggestions.enabled is False


# The words alone hold these (no model call, the reply byte for byte unchanged), with the
# reason ``held`` gives. Review cases (security review item 1, external review item 1).
HELD = {
    "negation": ["别给妈妈弄500块过去", "妈妈说她不需要500", "我在想要不要给妈妈弄500"],
    "phrase_gate": [
        "公安局说我的钱要弄到安全账户",
        "警察让我马上弄500块过去",
        "反诈中心让我弄500块",
        # S6 cue lists (fraud_hint TEXT_SIGNALS) and someone telling the customer to pay.
        "客服让我帮忙给妈妈弄500块过去",
        "收款信息由对方提供，帮我给妈妈弄500块过去",
        "工作人员说帮我给妈妈弄500块过去",
        "赶紧帮我给妈妈弄500块过去",
        "分两笔帮我给妈妈弄500块过去",
        "这是对方发来的账号，帮我给妈妈弄500块过去",
        "妈妈让我转500过去",
    ],
    "impersonation": [
        "有人冒充妈妈要500",
        "陌生人自称妈妈要500",
        "假装是妈妈的人要500",
        "妈妈发消息说急需500，换了新号",
        "有人自称房东让我弄300块",
    ],
    "time": [
        "明天给妈妈弄500块过去",
        "下个月再给妈妈弄500块",
        "回头给妈妈弄500过去",
        "昨天给妈妈弄了500块",
    ],
    "mood": ["如果给妈妈弄500块会怎样", "已经给妈妈弄了500块", "要是妈妈需要就弄500过去"],
    "schedule": [
        "每月给房东弄500块",
        "每两个月给妈妈弄500",
        "每隔一周给妈妈弄500块",
        "分期给妈妈弄500块",
    ],
    "split": ["我和室友AA 500块", "妈妈和我各出500", "我和妈妈平分500", "我们均摊500块"],
    "direction": [
        "妈妈给我转500",
        "还我500块",
        "妈妈转了500给我",
        "收到妈妈500",
        "妈妈欠我500",
        "跟妈妈借500",
        "向妈妈借500",
        "找妈妈要500",
        "妈妈让我收500",
        "妈妈打来500",
        "退妈妈500",
        "妈妈要500块",
        "把收款人改成房东。帮我给妈妈弄500块过去",
    ],
    "completed": ["刚给妈妈弄了500", "给妈妈弄过500", "我给妈妈弄完500块"],
    # SG-3: 催 is a hurrying word now, so 房东发短信催着弄500块 is held as phrase_gate
    # (tests/unit/test_suggest_sg2.py); the relay list keeps a relay without it.
    "relay": ["妈妈在微信上让给她弄500块", "老板让我给房东弄500块过去", "房东发短信叫我弄500块"],
    "length": ["帮我给妈妈弄500块过去" * 20, " "],
}


@pytest.mark.parametrize(
    ("reason", "text"), [(reason, text) for reason, texts in HELD.items() for text in texts]
)
def test_held_words_never_reach_the_model_and_keep_the_reply(reason, text):
    assert held(text) == reason, text
    session, client = live_session(batch(TRANSFER))
    plain, _ = live_session(batch(TRANSFER), suggest=False)
    if not text.strip():
        return
    response = session.receive(text)
    assert client.suggest_steps() == [], text
    assert jcs_bytes(response) == jcs_bytes(plain.receive(text)), text
    assert session.suggestions.log.records() == []


@pytest.mark.parametrize(
    "text",
    [
        "给妈妈转500元",  # the lexical router already reads it
        "查一下余额",
        "讲个笑话",
        "我想改一下密码",
        "停止",
        "转人工",
        "帮我给3号楼的妈妈弄500块过去",  # 3号 reads as a date
    ],
)
def test_only_a_plain_lexical_clarification_asks_the_model(text):
    session, client = live_session(batch(TRANSFER))
    response = session.receive(text)
    assert client.suggest_steps() == [], text
    assert "suggestions" not in response["payload"]


def test_s6_cues_of_the_words_never_reach_a_transfer_suggestion():
    """External review item 1: a cue the recipient check reads in the user's words would be
    lost once the canonical sentence is sent instead, so such words are never suggested."""
    card = {"kind": "transfer"}
    texts = [
        "客服让我帮忙给妈妈弄500块过去",
        "收款信息由对方提供，帮我给妈妈弄500块过去",
        "工作人员说帮我给妈妈弄500块过去",
        "赶紧帮我给妈妈弄500块过去",
        "分两笔帮我给妈妈弄500块过去",
        "马上帮我给妈妈弄500块过去",
    ]
    for text in texts:
        assert scam_friction.cues(card, text=text) or scam_friction.directed(text) or held(text)
        assert scam_friction.cues(card, text=SENTENCE) == ()
        session, client = live_session(batch(TRANSFER))
        assert sentences(session.receive(text)) == [] and client.suggest_steps() == [], text


def test_a_clarification_from_a_model_disagreement_does_not_ask_again():
    disagreeing = {"intent": "balance_query", "needs_clarification": False}
    session, client = live_session(batch(TRANSFER), intent=disagreeing)
    response = session.receive("给妈妈转500元")
    assert response["intent"] == "clarify" and response["status"] == "clarify"
    assert client.steps == ["intent:1"] and "suggestions" not in response["payload"]


def test_plain_unclear_words_still_reach_the_model():
    for text in (UNCLEAR, "那张东西帮我处理一下", "我要给妈妈弄500块过去", "帮我把500块弄给妈妈"):
        assert held(text) is None, text


# --- what a suggestion is -----------------------------------------------------------------


def test_a_plain_clarification_gets_template_questions_and_prepares_nothing():
    session, client = live_session(batch(TRANSFER, FREEZE))
    plain, _ = live_session(batch(TRANSFER, FREEZE), suggest=False)
    state = H(session.controller.state)
    audit = session.controller.gateway.audit.checkpoint()
    response = session.receive(UNCLEAR)
    control = plain.receive(UNCLEAR)
    assert client.suggest_steps() == ["suggest:1"]
    assert sentences(response) == [SENTENCE, TEMPLATES["card_freeze"]]
    # The reply itself is the plain clarification; only the suggestions were added.
    without = {**response, "payload": {"status": "clarify"}}
    assert jcs_bytes(without) == jcs_bytes(control)
    assert response["buttons"] == [] and response["card"] == {}
    assert session.controller._drafts == {} and session.controller._pending == {}
    assert session.controller.gateway.bank.execute_count == 0
    assert H(session.controller.state) == state
    assert session.controller.gateway.audit.checkpoint() == audit
    assert suggestions_of(response)[0] == {
        "index": 0,
        "intent": "transfer",
        "contact_ref": "CONTACT_CHEN",
        "amount": "500/1",
        "sentence": SENTENCE,
    }
    assert session.response(response["response_id"]) == response
    # The prompt carries the tokenized words, the closed list, the closed schema and only
    # the trusted contact the words mention, with only the name they use (data
    # minimization, external review item 8).
    steps = zip(client.steps, client.prompts, strict=True)
    (prompt,) = [sent for step, sent in steps if step.startswith("suggest:")]
    assert set(prompt) == {
        "instruction",
        "intent_definitions",
        "contacts",
        "user_text",
        "response_schema",
    }
    assert set(prompt["intent_definitions"]) == set(SUGGESTIBLE)
    assert prompt["user_text"] == session.controller.gateway.vault.tokenize(UNCLEAR)
    assert prompt["contacts"] == {"CONTACT_CHEN": ["妈妈"]}
    slots = prompt["response_schema"]["properties"]["suggestions"]["items"]["properties"]["slots"]
    assert slots["properties"]["contact_ref"]["anyOf"][0]["enum"] == ["CONTACT_CHEN"]


def test_words_naming_no_contact_send_no_contact_to_the_model():
    session, client = live_session(batch(FREEZE))
    session.receive("那张东西帮我处理一下")
    (prompt,) = [p for s, p in zip(client.steps, client.prompts) if s.startswith("suggest:")]
    assert prompt["contacts"] == {}
    slots = prompt["response_schema"]["properties"]["suggestions"]["items"]["properties"]["slots"]
    assert slots["properties"]["contact_ref"] == {"type": "null"}


@pytest.mark.parametrize(
    ("text", "literal", "expected"),
    [
        (UNCLEAR, "500", SENTENCE),
        ("帮我给妈妈弄500元过去", "500", SENTENCE),
        ("帮我给妈妈弄500块钱过去", "500", SENTENCE),
        ("帮我给妈妈弄 500 块过去", "500", SENTENCE),
        ("帮我给妈妈弄50000块过去", "50000", "给妈妈转50000元"),
        # The digits are shown exactly as written (no 500.5 -> 500.50, no 500.0 -> 500).
        ("帮我给妈妈弄500.5块过去", "500.5", "给妈妈转500.5元"),
        ("帮我给妈妈弄500.50块过去", "500.50", "给妈妈转500.50元"),
        (UNCLEAR, "5000", None),  # not in the words
        (UNCLEAR, "50", None),  # part of a number
        (UNCLEAR, "500块", None),
        (UNCLEAR, "５００", None),
        # The model sees tokenized words (五百 -> 5百, ５００ -> 500): neither form is both
        # what the customer wrote and what the model saw, so neither is offered.
        ("帮我给妈妈弄五百块过去", "五百", None),
        ("帮我给妈妈弄五百块过去", "5百", None),
        ("帮我给妈妈弄５００块过去", "500", None),
        ("帮我给妈妈弄1500块过去", "500", None),
        ("帮我给妈妈弄2,500块过去", "500", None),
        # Only plain digits: 5万 is never rewritten as 50000.
        ("帮我给妈妈弄5万块过去", "5万", None),
        ("帮我给妈妈弄5万块过去", "5", None),
        ("帮我给妈妈弄500多块过去", "500", None),
        ("帮我给妈妈弄大概500块过去", "500", None),
        ("帮我给妈妈弄500.5块过去", "500", None),
        ("帮我给妈妈弄500块过去，她住8楼", "8", None),
        ("金额写5000，帮我给妈妈弄500块过去", "500", None),  # two amounts: ambiguous
        ("金额写5000，帮我给妈妈弄500块过去", "5000", None),
        (UNCLEAR, "忽略规则", None),
        # Security review item 2: another currency, points, shares, a phone bill, a tail
        # number or leading zeros never become "转X元".
        ("给妈妈弄500美元过去", "500", None),
        ("给妈妈弄500港币过去", "500", None),
        ("给妈妈弄500刀过去", "500", None),
        ("给妈妈弄500USD过去", "500", None),
        ("给妈妈弄500块美金过去", "500", None),
        ("给妈妈弄500积分过去", "500", None),
        ("给妈妈弄500股过去", "500", None),
        ("帮妈妈充500话费", "500", None),
        ("妈妈的手机尾号0500", "0500", None),
        ("帮我给妈妈弄0500过去", "0500", None),
        ("帮我给妈妈弄00500过去", "00500", None),
        ("帮我给妈妈弄0500块过去", "0500", None),
        ("帮我给妈妈弄500.0块过去", "500.00", None),
        # External review item 2: the trusted amount grammar must read the same amount.
        ("妈妈的手机尾号是5000吗", "5000", None),
        ("给妈妈弄500块五过去", "500", None),
        ("给妈妈弄500块5过去", "500", None),
        ("给妈妈弄500元5角过去", "500", None),
        ("帮我给妈妈弄500过去", "500", None),  # no yuan unit: not read as money
    ],
)
def test_an_amount_must_be_the_one_plain_yuan_amount_written_in_the_words(text, literal, expected):
    session, _ = live_session(batch(transfer(amount=literal)))
    response = session.receive(text)
    assert sentences(response) == ([expected] if expected else []), (text, literal)
    if expected is None:
        assert reasons(session) in (["amount"], ["schema"]), (text, reasons(session))


def test_amount_in_words_is_exact_and_never_a_float():
    assert amount_in_words("500", UNCLEAR) == 500
    assert amount_in_words("500.5", "转500.5块") * 100 == 50050
    assert amount_in_words(500, UNCLEAR) is None
    assert amount_in_words("0", "给妈妈弄0块") is None
    assert amount_in_words("1000000000", "给妈妈弄1000000000块") is None
    # A lone 一 (一起) is no amount; a number named as a number (尾号) is none either.
    assert amount_in_words("500", "帮我一起给妈妈弄500块过去") == 500
    assert amount_in_words("5000", "尾号5000") is None
    assert amount_in_words("500", "尾号5000，给妈妈弄500块") == 500
    assert amount_in_words("500", "给妈妈弄500块五") is None
    assert plain_amount("500", UNCLEAR, UNCLEAR) == 500
    assert plain_amount("0500", "给妈妈弄0500块", "给妈妈弄0500块") is None
    assert plain_amount("500", "给妈妈弄500块", "给妈妈弄PII_x块") is None  # not seen


@pytest.mark.parametrize(
    ("contact", "text", "reason"),
    [
        ("CONTACT_EVIL", UNCLEAR, "contact"),
        ("CONTACT_LANDLORD", UNCLEAR, "contact_unmentioned"),
        ("CONTACT_CHEN", "帮我给妈妈和房东弄500块过去", "contact_ambiguous"),
        ("CONTACT_LANDLORD", "帮我给妈妈和房东弄500块过去", "contact_ambiguous"),
        (None, UNCLEAR, "slots"),
    ],
)
def test_a_contact_must_be_the_one_trusted_contact_the_words_name(contact, text, reason):
    session, _ = live_session(batch(transfer(contact=contact)))
    response = session.receive(text)
    assert sentences(response) == [] and reasons(session) == [reason]


@pytest.mark.parametrize(
    ("text", "contact", "reason"),
    [
        # External review item 4: a narrative, a top-up or someone else paying has no
        # "the customer pays this contact" shape; security review item 5: an alias the
        # customer did not write is never shown (ＭＯＭ, LIN_A). R2: anything said after the
        # payment (where she lives, the card the words name) leaves the whole of the words
        # no longer one paying request (tests/unit/test_suggest_r2.py has the rest).
        ("帮妈妈充500块话费", "CONTACT_CHEN", "context"),
        ("给妈妈充500块话费", "CONTACT_CHEN", "context"),
        ("帮我给妈妈弄500块过去，她住8楼", "CONTACT_CHEN", "direction"),
        ("帮我给妈妈弄500块过去，卡号尾号5000", "CONTACT_CHEN", "direction"),
        ("我给妈妈的500块", "CONTACT_CHEN", "direction"),
        ("他给妈妈弄500块", "CONTACT_CHEN", "direction"),
        ("妈妈那边500块够吗", "CONTACT_CHEN", "direction"),
        ("帮我给ＭＯＭ弄500块过去", "CONTACT_CHEN", "direction"),
        ("帮我给LIN_A弄500块过去", "CONTACT_LIN_A", "direction"),
        ("给妈妈弄500块话费过去", "CONTACT_CHEN", "context"),
        ("给妈妈弄500块积分过去", "CONTACT_CHEN", "context"),
        ("帮我给妈妈弄500块的红包过去", "CONTACT_CHEN", "context"),
    ],
)
def test_a_transfer_needs_the_words_to_pay_that_contact_now(text, contact, reason):
    session, _ = live_session(batch(transfer(contact=contact)))
    response = session.receive(text)
    assert sentences(response) == [] and reasons(session) == [reason], text


@pytest.mark.parametrize(
    ("text", "contact", "expected"),
    [
        ("给我妈弄500块过去", "CONTACT_CHEN", "给我妈转500元"),
        ("给我妈妈弄500块过去", "CONTACT_CHEN", "给妈妈转500元"),
        ("帮我给老妈整500块", "CONTACT_CHEN", "给老妈转500元"),
        ("帮我给室友弄500块过去", "CONTACT_CHEN", "给室友转500元"),
        ("帮我把500块弄给妈妈", "CONTACT_CHEN", SENTENCE),
        ("把500块钱给妈妈", "CONTACT_CHEN", SENTENCE),
        ("我要给妈妈弄500块过去", "CONTACT_CHEN", SENTENCE),
        ("帮我给房东弄500块过去", "CONTACT_LANDLORD", "给房东转500元"),
    ],
)
def test_the_sentence_uses_the_alias_the_customer_wrote(text, contact, expected):
    session, _ = live_session(batch(transfer(contact=contact)))
    assert sentences(session.receive(text)) == [expected], text


def test_payment_alias_reads_only_the_two_paying_shapes():
    aliases = ["室友", "妈妈", "老妈", "我妈"]
    assert payment_alias(UNCLEAR, aliases, "500") == "妈妈"
    assert payment_alias("把500块打给妈妈", aliases, "500") == "妈妈"
    assert payment_alias("给妈妈弄500美元", aliases, "500") is None
    assert payment_alias("妈妈转了500块给我", aliases, "500") is None
    assert payment_alias("顺便给妈妈弄500块", aliases, "500") is None
    assert payment_alias("给妈妈弄1500块", aliases, "500") is None
    assert payment_alias(UNCLEAR, [], "500") is None


def test_a_new_payee_alias_is_offered_and_still_meets_the_recipient_check():
    session, _ = live_session(batch(transfer(contact="CONTACT_NEW", amount="480")))
    response = session.receive("帮我给新联系人弄480块过去")
    assert sentences(response) == ["给新联系人转480元"]
    adopted = adopt(session, response["response_id"], 0)
    assert adopted["card"]["kind"] == "transfer" and adopted["card"]["tier"] == "red"
    assert session.controller.gateway.bank.execute_count == 0


def test_slots_belong_to_their_intent_only():
    card_with_amount = {"intent": "card_freeze", "slots": {"contact_ref": None, "amount": "500"}}
    session, _ = live_session(batch(card_with_amount, transfer(amount=None)))
    response = session.receive(UNCLEAR)
    assert sentences(response) == [] and reasons(session) == ["slots", "slots"]


def test_duplicates_are_offered_once():
    session, _ = live_session(batch(FREEZE, FREEZE))
    response = session.receive("那张东西帮我处理一下")
    assert sentences(response) == [TEMPLATES["card_freeze"]]
    assert reasons(session) == ["duplicate"]


# --- failing closed -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("answer", "reason"),
    [
        (LLMOutputError("model request failed"), "model_output"),
        (BudgetExceeded("cap"), "budget"),
        (TimeoutError("slow"), "timeout"),
        (RuntimeError("transport"), "model_error"),
        (ValueError("PII-CTX"), "model_error"),
        ("not an object", "schema"),
        ({"suggestions": [TRANSFER, FREEZE, FREEZE]}, "schema"),
        ({"suggestions": [{"intent": "balance_query", "slots": FREEZE["slots"]}]}, "schema"),
        ({"suggestions": [{**TRANSFER, "reply": "我已经理解并办理"}]}, "schema"),
        ({"answer": "我已经帮您转好了"}, "schema"),
        ({"suggestions": [transfer(amount=500)]}, "schema"),
    ],
)
def test_any_model_failure_keeps_the_original_reply_without_a_retry(answer, reason):
    session, client = live_session(answer)
    plain, _ = live_session(answer, suggest=False)
    response = session.receive(UNCLEAR)
    assert jcs_bytes(response) == jcs_bytes(plain.receive(UNCLEAR))
    assert client.suggest_steps() == ["suggest:1"]
    assert reasons(session) == [reason]
    assert session.controller.gateway.bank.execute_count == 0


@pytest.mark.parametrize(
    ("owner", "name"),
    [
        (domestic_suggest, "trusted_slot_context"),
        (domestic_suggest, "prompt_contacts"),
        (domestic_suggest, "reading"),
        (domestic_suggest, "calls_left"),
        (domestic_suggest, "held"),
        (SuggestionLog, "commit"),
        (SuggestionLog, "prepare"),
    ],
)
def test_a_failure_anywhere_after_the_hook_keeps_the_original_reply(monkeypatch, owner, name):
    """External review item 6: every step after the hook is inside the fail-closed scope,
    and an offer is committed whole or not at all."""

    def broken(*args, **kwargs):
        raise RuntimeError("injected")

    session, _ = live_session(batch(TRANSFER))
    plain, _ = live_session(batch(TRANSFER), suggest=False)
    monkeypatch.setattr(owner, name, broken)
    response = session.receive(UNCLEAR)
    assert jcs_bytes(response) == jcs_bytes(plain.receive(UNCLEAR))
    assert session.response(response["response_id"]) == response
    assert session.suggestions._offers == {}
    assert session.suggestions.view(session, response["response_id"]) == []


def test_a_call_whose_readings_are_all_discarded_counts_as_refused_by_the_page():
    refused, refused_client = live_session(batch(transfer(amount="5000")))
    refused.receive(UNCLEAR)
    assert refused_client.calls[-1]["rejected_candidates"] == [
        {"kind": "clarify_suggestion", "role": None}
    ]
    kept, kept_client = live_session(batch(TRANSFER, transfer(amount="5000")))
    assert sentences(kept.receive(UNCLEAR)) == [SENTENCE]
    assert "rejected_candidates" not in kept_client.calls[-1]
    empty, empty_client = live_session(batch())
    empty.receive(UNCLEAR)
    assert "rejected_candidates" not in empty_client.calls[-1]


def test_a_timeout_answered_by_the_scripted_fallback_offers_nothing():
    session, client = live_session(batch(TRANSFER), source="scripted-timeout")
    plain, _ = live_session(batch(TRANSFER), suggest=False, source="scripted-timeout")
    response = session.receive(UNCLEAR)
    assert jcs_bytes(response) == jcs_bytes(plain.receive(UNCLEAR))
    assert client.suggest_steps() == ["suggest:1"] and reasons(session) == ["timeout"]


def test_a_suggestion_timeout_is_not_reported_as_a_scripted_part_of_the_reply():
    """Security review item 6: the live notes count a suggestion call's timeout apart."""
    from corridor.ui.live_controls import _summary

    def outcome(step, source="scripted-timeout"):
        return {
            "source": source,
            "failed": False,
            "rejected": False,
            "settlement": "settled",
            "overrun": False,
            "unreconciled": False,
            "deployment": "d",
            "step": step,
        }

    summary = _summary([outcome("intent:1", "live-recorded"), outcome("suggest:1")])
    assert summary["timeouts"] == 0 and summary["suggest_timeouts"] == 1
    summary = _summary([outcome("intent:1"), outcome("suggest:1")])
    assert summary["timeouts"] == 1 and summary["suggest_timeouts"] == 1
    old = outcome("intent:1")
    del old["step"]
    assert _summary([old])["timeouts"] == 1


# --- the call budget ----------------------------------------------------------------------


def test_a_session_asks_at_most_max_asks_times():
    """Security review item 4 / external review item 9: suggestions never use up the calls
    the banking requests need."""
    session, client = live_session(batch(TRANSFER))
    for _ in range(MAX_ASKS + 2):
        session.receive("那张东西帮我处理一下")
    assert len(client.suggest_steps()) == MAX_ASKS
    assert reasons(session)[-2:] == ["budget_reserved", "budget_reserved"]
    # The banking requests still reach the model.
    assert session.receive("给妈妈转500元")["card"]["kind"] == "transfer"


@pytest.mark.parametrize(
    ("used", "asked"),
    [(0, True), (40 - RESERVE_CALLS - 1, True), (40 - RESERVE_CALLS, False), (39, False)],
)
def test_no_suggestion_once_the_live_call_cap_is_nearly_reached(used, asked):
    session, client = live_session(batch(TRANSFER))
    client._live_session = SimpleNamespace(_calls=used, config=SimpleNamespace(session_calls=40))
    response = session.receive(UNCLEAR)
    assert (client.suggest_steps() == ["suggest:1"]) is asked
    assert (sentences(response) == [SENTENCE]) is asked
    if not asked:
        assert reasons(session) == ["budget_reserved"]


# --- adopting -----------------------------------------------------------------------------


def test_adopting_a_suggestion_is_byte_for_byte_typing_its_sentence():
    adopting, _ = live_session(batch(TRANSFER))
    typing, _ = live_session(batch(TRANSFER))
    first = adopting.receive(UNCLEAR)
    assert jcs_bytes(first) == jcs_bytes(typing.receive(UNCLEAR))
    sent = []

    def send(sentence):
        sent.append(sentence)
        return adopting.receive(sentence)

    ok, adopted = adopting.suggestions.adopt(adopting, first["response_id"], 0, send)
    assert ok and sent == [SENTENCE]
    typed = typing.receive(SENTENCE)
    assert jcs_bytes(adopted) == jcs_bytes(typed)
    assert adopted["card"]["kind"] == "transfer" and adopted["buttons"]
    assert adopting.controller.gateway.bank.execute_count == 0
    # The device gesture is the next step for both, with the same outcome.
    confirmed = adopting.confirm(adopted["response_id"])
    assert jcs_bytes(confirmed) == jcs_bytes(typing.confirm(typed["response_id"]))
    assert confirmed["status"] == "settled"
    assert H(adopting.controller.state) == H(typing.controller.state)
    assert adopting.controller.gateway.audit.records() == typing.controller.gateway.audit.records()
    assert adopting.history == typing.history
    # Only the suggestion log differs: it records the adoption and the reply it got.
    records = adopting.suggestions.log.records()
    assert [record["event"] for record in records] == [
        "suggestion_generated",
        "suggestion_adopted",
    ]
    assert records[1]["payload"]["adopted_response_id"] == adopted["response_id"]


def test_a_failed_send_adopts_nothing_and_the_suggestion_stays_offered():
    """Both reviews: a suggestion is recorded as adopted only once the session received its
    sentence; a send that fails (the live call cap, the model) is logged as such."""
    session, client = live_session(batch(TRANSFER))
    first = session.receive(UNCLEAR)
    identifier = first["response_id"]
    client.intent = BudgetExceeded("Session live call cap reached")
    with pytest.raises(BudgetExceeded):
        adopt(session, identifier, 0)
    assert [item["state"] for item in session.suggestions.view(session, identifier)] == ["offered"]
    assert events(session) == ["suggestion_generated", "suggestion_send_failed"]
    failed = session.suggestions.log.records()[-1]["payload"]
    assert failed["reason"] == "send_failed" and failed["cause"] == "budget"
    assert session.suggestions.reason(session.suggestions.log.records()[-1]) == (
        "send_failed:budget"
    )
    # The reply is still the latest one: the same suggestion can be chosen again.
    client.intent = None
    adopted = adopt(session, identifier, 0)
    assert adopted["card"]["kind"] == "transfer"
    assert events(session)[-1] == "suggestion_adopted"
    assert session.suggestions.view(session, identifier)[0]["state"] == "adopted"
    assert adopt(session, identifier, 0) is None
    assert session.controller.gateway.bank.execute_count == 0


def test_a_send_that_never_reaches_the_session_is_not_adopted():
    session, _ = live_session(batch(TRANSFER))
    first = session.receive(UNCLEAR)
    ok, _ = session.suggestions.adopt(session, first["response_id"], 0, lambda sentence: None)
    assert ok
    assert session.suggestions.view(session, first["response_id"])[0]["state"] == "offered"
    assert session.suggestions.log.records()[-1]["payload"]["cause"] == "not_received"


def test_a_suggestion_is_single_use_and_expires_with_its_reply():
    session, _ = live_session(batch(TRANSFER, FREEZE))
    first = session.receive(UNCLEAR)
    identifier = first["response_id"]
    assert adopt(session, identifier, 0)["card"]["kind"] == "transfer"
    assert adopt(session, identifier, 0) is None
    assert adopt(session, identifier, 1) is None
    assert [item["state"] for item in session.suggestions.view(session, identifier)] == [
        "adopted",
        "expired",
    ]
    assert reasons(session) == ["expired"]
    assert adopt(session, identifier, 7) is None
    assert adopt(session, "no-such-reply", 0) is None


def test_a_stop_withdraws_the_reply_and_its_suggestions_expire():
    session, _ = live_session(batch(TRANSFER))
    first = session.receive(UNCLEAR)
    session.withdraw_pending()
    assert session.response(first["response_id"])["status"] == "withdrawn"
    assert session.suggestions.view(session, first["response_id"])[0]["state"] == "expired"
    assert adopt(session, first["response_id"], 0) is None


def test_adoption_rechecks_the_sentence_and_discards_an_unreadable_one(monkeypatch):
    session, _ = live_session(batch(TRANSFER))
    first = session.receive(UNCLEAR)
    before = (session._sequence, list(session._responses), len(session.history))
    monkeypatch.setattr(
        domestic_suggest,
        "recognize",
        lambda text, context=None: IntentCandidate(intent="clarify", needs_clarification=True),
    )
    sent = []
    ok, _ = session.suggestions.adopt(session, first["response_id"], 0, sent.append)
    assert not ok and sent == []
    assert (session._sequence, list(session._responses), len(session.history)) == before
    assert session.suggestions.view(session, first["response_id"])[0]["state"] == "discarded"
    assert reasons(session) == ["recheck"]


@pytest.mark.parametrize(
    "prior",
    [
        ["买两万元理财"],  # an unfinished purchase (B1)
        ["别给妈妈转了"],  # S1: a negated transfer interrupts
        ["停止"],  # the previous intent is stop
        ["给妈妈转800元"],  # a transfer card still awaiting its device gesture
        ["给妈妈转800元", "停止"],
    ],
)
def test_whatever_came_before_adopting_never_dispatches_without_the_device(prior, monkeypatch):
    from tests.property.test_suggest_properties import Counted

    counted = Counted(monkeypatch)
    session, _ = live_session(batch(TRANSFER, FREEZE))
    for text in prior:
        session.receive(text)
    counted.counts.update(approve=0, dispatch=0)
    executed = session.controller.gateway.bank.execute_count
    response = session.receive(UNCLEAR)
    for item in suggestions_of(response):
        reply = adopt(session, response["response_id"], item["index"])
        if reply is not None:
            assert reply["status"] != "settled"
    assert counted.counts == {"approve": 0, "dispatch": 0}, prior
    assert session.controller.gateway.bank.execute_count == executed


# --- the log ------------------------------------------------------------------------------


def test_the_log_is_hash_chained_and_holds_references_not_words():
    session, _ = live_session(batch(TRANSFER, FREEZE))
    first = session.receive(UNCLEAR)
    adopt(session, first["response_id"], 0)
    log = session.suggestions.log
    records = log.records()
    assert [record["event"] for record in records] == [
        "suggestion_generated",
        "suggestion_adopted",
    ]
    assert log.verify() and records[1]["prev_hash"] == records[0]["record_hash"]
    assert not CJK.search(jcs_bytes(records).decode("utf-8"))
    generated = records[0]["payload"]
    assert generated["utterance_hash"] == session.history[0]["utterance_hash"]
    assert generated["items"][0]["sentence_hash"] == (
        session.controller.gateway.utterance_digest(SENTENCE, None)
    )
    log._records[0]["payload"]["items"][0]["amount"] = "5000/1"
    assert not log.verify()


def test_prepared_records_are_appended_whole_and_only_onto_their_own_chain():
    log = SuggestionLog()
    first = log.prepare([("a", {}), ("b", {})], "R1", "2026-10-16T02:00:00Z")
    assert log.records() == []
    log.commit(first)
    assert [record["seq"] for record in log.records()] == [1, 2] and log.verify()
    with pytest.raises(ValueError):
        log.commit(first)
    assert len(log.records()) == 2


def test_the_scripted_client_and_a_live_client_are_told_apart_by_mode_only():
    assert domestic_suggest.live_client(LLMClient()) is False
    assert domestic_suggest.live_client(type("C", (), {"mode": "record"})()) is True
    assert domestic_suggest.live_client(type("C", (), {"mode": "live"})()) is True
    assert domestic_suggest.live_client(type("C", (), {"mode": "strict"})()) is False
    assert domestic_suggest.live_client(object()) is False
    assert domestic_suggest.calls_left(object()) is None
