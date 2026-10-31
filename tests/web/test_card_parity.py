"""Card fact parity: fact map, Streamlit ``render_card`` and the web ``CardVM`` (§3.5.2).

For every card of the corpus (the authoritative cards of the equivalence flows, the
redisplayed card, the R4 refusal card and a purchase card from ``compare_investments``)
and for both views and both languages, three ``(field path, label, value)`` sequences
must be equal item by item and in order: the independent ``fact_map``, the rows
Streamlit draws (``components.st`` recorded, each row of ``_fact_rows_html`` read back
with the field it came from) and the web ``CardVM.facts``. The amount line, the notes
drawn outside the frame and the "why" lines are compared the same way; in the judge
view the technical JSON is compared by its key set.
"""

import re
from contextlib import contextmanager
from dataclasses import dataclass
from functools import cache
from html import unescape

import pytest

from corridor.agent.domestic_session import DomesticSession
from corridor.channels.im import IMAdapter
from corridor.sim.device_workflow import business_facts
from corridor.ui import components, explanations, scam_friction
from corridor.ui.controller import DemoController, demo_state
from corridor.ui.messages import tr
from corridor.ui.wealth_controller import WealthController
from tests.unit.test_ui_scam_friction import (
    CLARIFY,
    CLARIFY_PLAIN,
    FRAUD,
    KNOWN_AUTHORITY,
    NEW,
    SAFE,
    SCHEDULED_NEW,
)
from tests.unit.test_ui_scam_r7 import DIRECTED_NEW, OPENING
from tests.web.fact_map import (
    FACT_MAP,
    BrokenCard,
    expected_amount,
    expected_facts,
    expected_note_kinds,
    expected_object_value,
    in_view,
)
from tests.web.flows import (
    BIRTHDAY,
    CARD_LOST,
    RED,
    SCHEDULED,
    STOP,
    SUBSCRIPTIONS,
    URGENT,
    YELLOW,
)

VIEWS = ("customer", "judge")
LOCALES = ("zh-CN", "en")
ROW = re.compile(
    r'<div class="fact-row"><div class="fact-label">(.*?)</div>'
    r'<div class="fact-value">(.*?)</div></div>',
    re.DOTALL,
)
OBJECT_ROWS = {
    "contact_ref",
    "product_ref",
    "merchant_ref",
    "source_ref",
    "locked_ref",
    "participant_refs",
}


@dataclass
class Entry:
    card: dict
    controller: object
    hint: str | None = None
    files: int = 0
    canonical: bool = True
    position: int | None = None
    total: int | None = None


# --- corpus -------------------------------------------------------------------------------


def _fresh(state=None):
    controller = DemoController(initial_state=state or demo_state(showcase=True))
    wealth = WealthController()
    controller.start_session()
    wealth.start_session()
    return controller, DomesticSession(controller, wealth)


def _chat(text):
    controller, session = _fresh()
    response = session.receive(text)
    hint = controller.gateway.vault.tokenize(text)
    return controller, session, response, hint


def _identity(response):
    payload = response["payload"]
    return payload.get("pending_id") or payload.get("draft_id") or payload.get("plan_id")


def chat_cards(text):
    controller, _, response, hint = _chat(text)
    cards = controller._authoritative_cards(_identity(response))
    plan = len(cards) > 1
    return [
        Entry(
            card,
            controller,
            hint,
            canonical=not plan,
            position=index + 1 if plan else None,
            total=len(cards) if plan else None,
        )
        for index, card in enumerate(cards)
    ]


def _answer_self_check(controller, identity, text):
    """Use the page's answer/retry events before any protected confirmation."""
    status = controller.scam_gate_establish(identity, text=text)
    if status is None:
        return
    if status["decision"] == "pause":
        status = controller.scam_gate_retry(
            identity, card_identity=status["card_identity"], epoch=status["epoch"]
        )
    status = controller.scam_gate_answer(
        identity,
        card_identity=status["card_identity"],
        epoch=status["epoch"],
        answers=SAFE,
    )
    assert status["decision"] == "proceed" and status["reason"] is None


def prepared_cards(text):
    """The display card of the prepared action after the first confirmation."""
    controller, session, response, hint = _chat(text)
    _answer_self_check(controller, _identity(response), text)
    IMAdapter(session).click("confirm", response["response_id"])
    pending = session.response(response["response_id"])["payload"]["pending_id"]
    return [Entry(card, controller, hint) for card in controller._authoritative_cards(pending)]


def clarified_card(text, contact):
    controller, _, response, hint = _chat(text)
    draft = response["payload"]["draft_id"]
    controller.select_contact(draft, contact)
    return [Entry(card, controller, hint) for card in controller._authoritative_cards(draft)]


def redisplayed_card():
    """A draft-mode pending whose confirmation finds a changed fact gets a new card
    (the drift of ``test_redisplay_of_a_draft_pending_inherits_the_gate...``)."""
    controller, session, response, hint = _chat(SCHEDULED_NEW)
    old = _identity(response)
    before = controller._authoritative_cards(old)
    approve = controller.workflow._approve
    drift = {"left": 1}

    def changed(gateway, prepared, expected, display_id):
        if drift["left"]:
            drift["left"] -= 1
            prepared.proposal.card["amount"] = "301/1"
        return approve(gateway, prepared, expected, display_id)

    controller.workflow._approve = changed
    _answer_self_check(controller, old, SCHEDULED_NEW)
    result = session.confirm(response["response_id"])
    pending = result["payload"]["pending_id"]
    after = controller._authoritative_cards(pending)
    assert result["status"] == "redisplay" and pending != old
    assert [business_facts(card) for card in after] != [business_facts(card) for card in before]
    return [Entry(card, controller, hint) for card in after]


def r4_card():
    from corridor.ui.domestic_demos import prepare_demo

    controller = prepare_demo("suitability_r4")["controller"]
    (pending,) = controller._pending
    return [Entry(card, controller) for card in controller._authoritative_cards(pending)]


def purchase_card():
    controller, _ = _fresh()
    controller.compare_investments("net_return")
    pending = controller.preview_investment(request_id="PARITY-PURCHASE")["pending_id"]
    return [Entry(card, controller) for card in controller._authoritative_cards(pending)]


def compensation_card():
    """The task panel's compensation card after a confirmed and stopped birthday plan."""
    controller, session, response, _ = _chat(BIRTHDAY)
    nodes = [node["id"] for node in response["payload"]["nodes"]]
    IMAdapter(session).click("confirm", response["response_id"], selected_node_ids=nodes)
    session.receive(STOP)
    plan_id = controller.snapshot()["plans"][-1]["plan"]["plan_id"]
    pending = controller.compensate(plan_id)["pending_id"]
    return [Entry(card, controller) for card in controller._authoritative_cards(pending)]


def redemption_card():
    controller, _ = _fresh()
    (position,) = [item["id"] for item in controller.snapshot()["positions"]]
    pending = controller.redeem(position, request_id="PARITY-REDEEM")["pending_id"]
    return [Entry(card, controller) for card in controller._authoritative_cards(pending)]


def subscription_cancel_card():
    controller, _ = _fresh()
    query = controller.preview_subscriptions(request_id="PARITY-SUBS")
    (reference, *_) = controller.approve_pending(query["pending_id"])["cancellable"]
    pending = controller.cancel_subscription(reference, request_id="PARITY-SUBS-CANCEL")
    return [
        Entry(card, controller) for card in controller._authoritative_cards(pending["pending_id"])
    ]


def schedule_cancel_card():
    """A real standing authorization, then its prepared revocation card."""
    controller, session, response, _ = _chat(SCHEDULED)
    session.confirm(response["response_id"])
    session.verify_mfa(response["response_id"])
    assert session.confirm(response["response_id"])["status"] == "controlled"
    (reference,) = [
        key
        for key, item in controller.state.authorizations.items()
        if getattr(item, "authorization_type", None) == "spa"
    ]
    pending = controller.cancel_schedule(reference, request_id="PARITY-SCHEDULE-CANCEL")
    return [
        Entry(card, controller) for card in controller._authoritative_cards(pending["pending_id"])
    ]


CORPUS = {
    "yellow": lambda: chat_cards(YELLOW),
    "red": lambda: chat_cards(RED),
    "red_prepared": lambda: prepared_cards(RED),
    "urgent": lambda: chat_cards(URGENT),
    "new_payee": lambda: chat_cards(NEW),
    "known_authority": lambda: chat_cards(KNOWN_AUTHORITY),
    "fraud": lambda: chat_cards(FRAUD),
    "fraud_prepared": lambda: prepared_cards(FRAUD),
    "directed": lambda: chat_cards(DIRECTED_NEW),
    "opening": lambda: chat_cards(OPENING),
    "clarify_lin_b": lambda: clarified_card(CLARIFY, "CONTACT_LIN_B"),
    "clarify_lin_a": lambda: clarified_card(CLARIFY_PLAIN, "CONTACT_LIN_A"),
    "card_lost": lambda: chat_cards(CARD_LOST),
    "redisplay": redisplayed_card,
    "r4_refusal": r4_card,
    "purchase": purchase_card,
    "birthday_plan": lambda: chat_cards(BIRTHDAY),
    "subscriptions": lambda: chat_cards(SUBSCRIPTIONS),
    "scheduled_monthly": lambda: chat_cards(SCHEDULED),
    "compensation": compensation_card,
    "redemption": redemption_card,
    "subscription_cancel": subscription_cancel_card,
    "schedule_cancel": schedule_cancel_card,
}


@cache
def corpus(name):
    entries = CORPUS[name]()
    assert entries, name
    return entries


# --- the two renderers --------------------------------------------------------------------


class Recorder:
    """A stand-in for ``st`` that keeps every call with its expander depth."""

    def __init__(self):
        self.session_state = {}
        self.calls = []
        self.depth = 0

    @contextmanager
    def expander(self, label, **kwargs):
        self.calls.append((self.depth, "expander", str(label)))
        self.depth += 1
        try:
            yield
        finally:
            self.depth -= 1

    def __getattr__(self, name):
        def record(value=None, *args, **kwargs):
            self.calls.append((self.depth, name, value))

        return record


def seen(text):
    """The text a reader sees: markup removed, entities resolved, Markdown bold dropped."""
    text = re.sub(r"<br\s*/?>", "\n", str(text))
    text = re.sub(r"<[^<>]*>", "", text)
    return unescape(text).replace("**", "").strip()


def _between(markup, css):
    found = re.search(rf'<div class="{css}">(.*?)</div>', markup, re.DOTALL)
    return seen(found.group(1)) if found else None


def _items(markup):
    return [seen(item) for item in re.findall(r"<li>(.*?)</li>", markup, re.DOTALL)]


def streamlit_card(entry, monkeypatch):
    """Render one card with the real ``render_card`` and read back what it drew."""
    recorder, rows = Recorder(), []
    original = components._fact_rows_html

    def recorded(facts, *, controller=None):
        drawn = []
        for key, value in facts.items():
            part = original({key: value}, controller=controller)
            if part:
                label, shown = ROW.fullmatch(part).groups()
                path = f"objects.{key}" if key in OBJECT_ROWS else key
                drawn.append((path, unescape(label), unescape(shown)))
        rows.append(drawn)
        return original(facts, controller=controller)

    with monkeypatch.context() as patch:
        patch.setattr(components, "st", recorder)
        patch.setattr(explanations, "st", recorder)
        patch.setattr(components, "_fact_rows_html", recorded)
        components.render_card(
            entry.card,
            canonical=entry.canonical,
            position=entry.position,
            total=entry.total,
            controller=entry.controller,
            hint_text=entry.hint,
            external_files=entry.files,
        )
    return _read_back(recorder.calls, rows[0])


def _read_back(calls, facts):
    top = [(index, name, value) for index, (depth, name, value) in enumerate(calls) if not depth]
    first = next(index for index, name, value in top if name == "markdown")
    card = calls[first][2]
    result = {
        "facts": facts,
        "title": _between(card, "fact-title"),
        "purpose": _between(card, "approval-purpose"),
        "amount": _between(card, "fact-amount"),
        "tier": seen(re.search(r'<span class="tier[^"]*">(.*?)</span>', card).group(1)),
        "reason": seen(re.search(r'<p class="approval-reason">(.*?)</p>', card).group(1)),
        "notes": [],
        "why": [],
        "technical": None,
    }
    number = re.search(r'<span class="approval-number">(.*?)</span>', card)
    result["position"] = seen(number.group(1)) or None if number else None
    expanders = {}
    for position, (index, name, value) in enumerate(top):
        if index <= first:
            continue
        if name == "expander":
            end = top[position + 1][0] if position + 1 < len(top) else len(calls)
            expanders[value] = calls[index + 1 : end]
            continue
        note = _note(name, value)
        if note:
            result["notes"].append(note)
    why = expanders.get(tr("Why this action · rules, facts and audit"), [])
    result["why"] = [
        seen(value)
        for position, (_, name, value) in enumerate(why)
        if name in ("caption", "write")
        and not (position + 1 < len(why) and why[position + 1][1] == "json")
    ]
    technical = expanders.get(tr("Technical evidence"))
    if technical is not None:
        result["technical"] = [value for _, name, value in technical if name == "json"][-1]
    return result


def _note(name, value):
    """A call drawn outside the card frame, as (kind, title, lines, footer)."""
    text = str(value)
    if name == "markdown" and 'class="suit-block"' in text:
        title, footer = _between(text, "suit-title"), _between(text, "suit-footer")
        return ("suitability", title, _items(text), footer)
    if name == "markdown" and 'class="fraud-hint"' in text:
        footer, judge = _between(text, "fraud-footer"), _between(text, "fraud-judge")
        footer = f"{footer} {judge}" if judge else footer
        return ("fraud", _between(text, "fraud-title"), _items(text), footer)
    if name == "caption" and text == tr("Using the previous recipient"):
        return ("recipient_context", None, [seen(text)], None)
    if name == "markdown" and 'class="danger"' in text:
        return ("irreversible", None, [seen(text)], None)
    if name == "warning":
        return ("red", None, [seen(text)], None)
    if name == "caption":
        return ("schedule_scope", None, [seen(text)], None)
    return None


def web_card(entry):
    from corridor.web.vm.cards import card_vm

    vm = card_vm(
        entry.card,
        controller=entry.controller,
        key="TR1-0",
        canonical=entry.canonical,
        position=entry.position,
        total=entry.total,
        hint_text=entry.hint,
        external_files=entry.files,
    )
    vm["facts_seq"] = [(fact["key"], fact["label"], fact["value"]) for fact in vm["facts"]]
    vm["notes_seq"] = [
        (note["kind"], note["title"], note["lines"], note["footer"]) for note in vm["notes"]
    ]
    return vm


# --- the comparisons ----------------------------------------------------------------------

CASES = [(name, audience, locale) for name in CORPUS for audience in VIEWS for locale in LOCALES]


# P-DIFF-26 keeps subscription/holding references at the top of the web card. The
# integrated product now displays these objects too, at the derived-fact position.
# Values on both pages must agree with the same independent trusted-state expectation.
# P-DIFF-27 keeps the web judge view's red tier in the customer wording (the SMS frame
# is not an independent channel).
SIGNED_OBJECTS = {"cancel_subscription": "authorization_ref", "redeem_product": "position_ref"}


def signed_row(entry, field, audience, locale):
    """The extra row, from the card's own reference and the synthetic state alone."""
    from corridor.web.catalog import WEB_MESSAGES

    reference = entry.card["objects"][field]
    value = expected_object_value(entry.card, field, entry.controller)
    if audience == "judge":
        value = f"{value} · {reference}"
    return (f"objects.{field}", WEB_MESSAGES[f"web.vm.fact.{field}"][locale], value)


def web_expectation(entry, audience, locale, expected, drawn, monkeypatch):
    rows = [row for row in expected if row[0] not in {"subscription_object", "position_object"}]
    field = SIGNED_OBJECTS.get(entry.card.get("kind"))
    if field:
        rows.insert(0, signed_row(entry, field, audience, locale))
    head = (drawn["title"], drawn["purpose"], drawn["tier"], drawn["reason"])
    notes, why = list(drawn["notes"]), list(drawn["why"])
    if audience == "judge" and entry.card.get("tier") == "red":
        with in_view("customer", locale):
            plain = streamlit_card(entry, monkeypatch)
        head = (drawn["title"], drawn["purpose"], plain["tier"], plain["reason"])
        red = next(note for note in plain["notes"] if note[0] == "red")
        notes = [red if note[0] == "red" else note for note in notes]
        why = [line.replace(drawn["tier"], plain["tier"]) for line in why]
    return rows, head, notes, why


@pytest.mark.parametrize(("name", "audience", "locale"), CASES)
def test_card_facts_agree_three_ways(name, audience, locale, monkeypatch):
    for index, entry in enumerate(corpus(name)):
        where = f"{name}[{index}] {entry.card.get('kind')} {audience} {locale}"
        with in_view(audience, locale):
            expected = expected_facts(entry.card, entry.controller)
            amount = expected_amount(entry.card)
            drawn = streamlit_card(entry, monkeypatch)
            web = web_card(entry)
            kinds = expected_note_kinds(
                entry.card, entry.controller, hint_text=entry.hint, external_files=entry.files
            )
            rows, head, notes, why = web_expectation(
                entry, audience, locale, expected, drawn, monkeypatch
            )
        assert drawn["facts"] == expected, where
        assert web["facts_seq"] == rows, where
        assert drawn["amount"] == web["amount_text"] == amount, where
        assert [note[0] for note in drawn["notes"]] == kinds, where
        assert [note[0] for note in web["notes_seq"]] == kinds, where
        assert web["notes_seq"] == [
            (kind, title, lines, footer) for kind, title, lines, footer in notes
        ], where
        assert web["why"]["lines"] == why, where
        assert (web["title"], web["purpose"], web["tier"]["label"], web["tier_reason"]) == head, (
            where
        )
        if entry.card.get("tier") == "red":
            red = next(lines for kind, _, lines, _ in web["notes_seq"] if kind == "red")
            text = " ".join([web["tier"]["label"], web["tier_reason"], *red])
            assert "独立" not in text and "independent" not in text, where
        assert web.get("position") == drawn["position"], where
        if audience == "judge":
            assert set(web["technical"]["card"]) == set(drawn["technical"]) == set(entry.card)
        else:
            assert "technical" not in web and "structured" not in web, where
            assert drawn["technical"] is None, where


def test_the_corpus_covers_every_mapped_kind_it_claims():
    kinds = {entry.card.get("kind") for name in CORPUS for entry in corpus(name)}
    assert kinds <= set(FACT_MAP), kinds - set(FACT_MAP)
    assert {"transfer", "report_card_lost", "purchase_product", "schedule_transfer"} <= kinds
    assert {"lock_funds", "unlock_funds", "subscription_query", "redeem_product"} <= kinds
    assert {"cancel_subscription", "cancel_schedule"} <= kinds


@pytest.mark.parametrize("text", [NEW, FRAUD], ids=["new-payee", "fraud-cues"])
def test_an_unanswered_self_check_never_prepares_a_pending_payment(text):
    controller, session, response, _ = _chat(text)
    before = set(controller._pending)
    result = IMAdapter(session).click("confirm", response["response_id"])
    assert result["status"] == scam_friction.STATUS
    assert set(controller._pending) == before
    assert controller.gateway.bank.execute_count == 0
    gate = controller.scam_gate_status(_identity(session.response(response["response_id"])))
    assert gate["decision"] == "incomplete"
    assert gate["reason"] == scam_friction.REASON_INCOMPLETE
    assert not gate["challenge_issued"]


def test_a_card_without_its_recipient_is_reported_as_broken():
    entry = corpus("yellow")[0]
    broken = {**entry.card, "objects": {**entry.card["objects"], "contact_ref": None}}
    with in_view("judge", "zh-CN"), pytest.raises(BrokenCard):
        expected_facts(broken, entry.controller)


# --- injectivity: different business facts never read the same ---------------------------


def _reading(card, controller):
    with in_view("customer", "zh-CN"):
        rows = [(path, value) for path, _, value in expected_facts(card, controller)]
        return [("amount", expected_amount(card)), *rows]


def _objects(**changes):
    return lambda card: {**card, "objects": {**card["objects"], **changes}}


MUTATIONS = {
    "recipient": _objects(contact_ref="CONTACT_LIN_A"),
    "source": _objects(source_ref="A_LOCKED"),
    "amount": lambda card: {**card, "amount": "501/1"},
    "fees": lambda card: {**card, "fees": "2/1"},
    "window": lambda card: {**card, "execution_window": {"start": "2026-10-20T00:00:00Z"}},
    "total_after": lambda card: {**card, "total_after": "600/1"},
}


@pytest.mark.parametrize("field", sorted(MUTATIONS))
def test_a_changed_business_fact_changes_what_both_pages_show(field, monkeypatch):
    entry = corpus("yellow")[0]
    changed = Entry(MUTATIONS[field](entry.card), entry.controller, entry.hint)
    assert _reading(changed.card, entry.controller) != _reading(entry.card, entry.controller)
    with in_view("customer", "zh-CN"):
        before = streamlit_card(entry, monkeypatch)
        after = streamlit_card(changed, monkeypatch)
        web_before, web_after = web_card(entry), web_card(changed)
    shown = (before["amount"], before["facts"]), (after["amount"], after["facts"])
    assert shown[0] != shown[1]
    assert (web_before["amount_text"], web_before["facts_seq"]) != (
        web_after["amount_text"],
        web_after["facts_seq"],
    )


# --- the opening line: the anti-fraud opening on both pages ------------------------------


@pytest.mark.parametrize("text", [OPENING, FRAUD], ids=["opening", "fraud"])
def test_the_first_reply_line_is_the_same_anti_fraud_opening(text, monkeypatch):
    from corridor.ui import scam_friction
    from corridor.ui.domestic_views import response_copy
    from tests.unit.test_ui_scam_friction import shown_text
    from tests.web.drivers import StreamlitDriver, WebDriver

    monkeypatch.setenv("CORRIDOR_LOCALE", "zh-CN")
    page = StreamlitDriver()
    page.send(text)
    hint = page._state("domestic_transcript")[-1]["text"]
    response = page.session.response(page.rid)
    with in_view("judge", "zh-CN"):
        copy = response_copy(response, completion=True, hint_text=hint)
        opening = scam_friction.opening(scam_friction.opening_cues(response["card"], hint))
    assert copy in shown_text(page.page)
    web = WebDriver(view="judge", locale="zh-CN")
    try:
        web.send(text)
        lines = web.state["chat"]["responses"][web.rkey]["copy_lines"]
    finally:
        web.close()
    assert lines[0] == copy.splitlines()[0] == opening.splitlines()[0]
