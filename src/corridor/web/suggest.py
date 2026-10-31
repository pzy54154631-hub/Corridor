"""Model suggestions under a chat clarification: the list, its version and the choice.

The session's suggestion desk (``agent/domestic_suggest.py``) decides what is offered and
holds the canonical sentences; nothing here reads text from the browser. A choice names
one listed item (``c<i>``) and the version of the list the page showed; the desk checks
the sentence again and gives it back, and it is then sent exactly as a typed message
(``service._submit``): the same reply, card, gate and device approval as typing it. The
desk marks the suggestion adopted only once the session received the sentence; a failed
send (the live channel busy or failing) is logged and leaves it offered.

The desk is a sub-object of the session, so the session's proxy does not count its calls.
Its reads (``view``, ``log.records``, ``reason``) are made on the desk itself and are on
the reviewed list of ``tests/web/test_web_guards.py``. ``adopt``, which marks the
suggestion and appends to the desk's log before and after the send, is made only through
a proxy that counts into the session's own ``Effects`` with no allow list (RW-5 item 2):
a refusal raised after it is an unknown result, and the session is distrusted.
"""

from typing import Any

from corridor.web import compat, service, tasks
from corridor.web import state as web_state
from corridor.web.vm.cards import judge
from corridor.web.vm.text import plain

STATES = frozenset({"offered", "adopted", "discarded", "expired"})


def _counted(ws) -> Any:
    """The session's desk behind a proxy that counts every method call (no allow list)."""
    return web_state.Product(ws.session.suggestions, ws.effects, frozenset())


def choices(ws, rid: str) -> tuple[list[dict[str, Any]], str]:
    """The reply's suggestions as the desk shows them now, and the version binding a
    click to exactly this list (its order, sentences and states)."""
    desk = getattr(ws.session, "suggestions", None)
    # A read: ``view`` projects the desk's offers and whether the reply is still open.
    items = desk.view(ws.session, rid) if desk is not None else []
    return items, tasks.list_version(ws, f"chat-suggestion:{rid}", [{"items": items}])


def choose(ws, rkey: str, choice: str, list_version: str, client_msg_id: str) -> str:
    """Send one listed suggestion's sentence as a new message; never approve anything."""
    rid = service._rid(ws, rkey)
    items, version = choices(ws, rid)
    item = service._pick(items, choice)
    digest = compat.H({"kind": "message", "text": item["sentence"], "external_files": 0})
    replayed = service._replayed(ws, client_msg_id, digest)
    if replayed is not None:
        return replayed
    service._logged_in(ws)
    service._current(ws, rkey)
    if not service._same(version, list_version):
        raise service._fail("card_changed")
    if item["state"] != "offered":
        raise service._fail("not_applicable")
    # Counted before it runs: from here on, a refusal is an unknown result.
    sent, chosen = _counted(ws).adopt(
        ws.session,
        rid,
        item["index"],
        lambda sentence: service._submit(ws, sentence, client_msg_id, digest),
    )
    if not sent:
        # Checked again and discarded: nothing was sent, and the desk's log says so. The
        # desk changed state, so this refusal leaves ``Write.run`` as an unknown result.
        raise service._fail("not_applicable")
    return chosen


def _judge_lines(ws, rid: str) -> list[str]:
    payload = ws.session.response(rid)["payload"].get("suggestions") or {}
    shown = [plain(compat.tr("suggest.judge.source"))]
    for item in payload.get("items", []):
        shown.append(
            plain(
                compat.tr(
                    "suggest.judge.item",
                    number=item["index"] + 1,
                    intent=item["intent"],
                    contact=item["contact_ref"] or "—",
                    amount=item["amount"] or "—",
                )
            )
        )
    # Reads: the log's records are a deep copy, and ``reason`` is a function of the record.
    desk = ws.session.suggestions
    for record in desk.log.records(rid):
        shown.append(
            plain(
                compat.tr(
                    "suggest.judge.event",
                    seq=record["seq"],
                    event=record["event"],
                    reason=desk.reason(record),
                    hash=record["record_hash"][:16],
                )
            )
        )
    return shown


def vm(ws, rid: str, *, current: bool) -> dict[str, Any] | None:
    """``suggestion_select`` of a reply, or None when the model suggested nothing."""
    items, version = choices(ws, rid)
    if not items:
        return None
    shown: dict[str, Any] = {
        "note": plain(compat.tr("suggest.note")),
        "choices": [
            {
                "key": f"c{index}",
                "label": plain(compat.tr("suggest.question", sentence=item["sentence"])),
                "state": item["state"] if item["state"] in STATES else "expired",
                "state_text": (
                    plain(compat.tr("suggest.state." + item["state"]))
                    if item["state"] in STATES - {"offered"}
                    else None
                ),
            }
            for index, item in enumerate(items)
        ],
        "list_version": version,
        "locked": not current or bool(ws.untrusted),
    }
    if judge():
        shown["evidence_lines"] = _judge_lines(ws, rid)
    return shown
