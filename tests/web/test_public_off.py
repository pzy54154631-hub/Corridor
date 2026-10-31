"""Without public mode the service is what it was (design §3.3, T-0; RW-7 item 3).

``tests/web/golden/public-off-wire.json`` was recorded by ``record_public_off`` before any
public-mode code existed and recorded again only for the deliberate changes its ``history``
names (RW-9 item 7). These tests build the application without a ``public`` setting, run
the same journey and compare status codes,
response headers, cookie attributes and bodies response by response, with only the
secret-drawn fields the recorder names normalized. The route table and the entry's
``SERVER_OPTIONS`` are compared to their recorded values as well.
"""

import json

import pytest

from tests.web import record_public_off
from tests.web.conftest import serve

RECORDED = record_public_off.golden()


def test_the_golden_names_its_commit_and_what_it_normalizes():
    assert len(RECORDED["recorded_at_commit"]) == 40
    assert RECORDED["normalized"] == list(record_public_off.NORMALIZED)
    assert RECORDED["keyed"] == list(record_public_off.KEYED)
    assert RECORDED["dropped_headers"] == list(record_public_off.DROPPED_HEADERS)
    assert len(RECORDED["responses"]) >= 28
    statuses = {response["status"] for response in RECORDED["responses"]}
    assert {200, 400, 401, 403, 404, 405, 409, 413, 422} <= statuses


def test_the_golden_says_why_it_was_recorded_again():
    """RW-9 item 7: the recordings after the first moved ``/api/i18n`` alone, for the login
    card's six strings, added the two keys of the contact hint (RW-11) and then reworded it."""
    assert RECORDED["history"] == list(record_public_off.HISTORY)
    first, *later = RECORDED["history"]
    assert first["commit"].startswith("2273589") and "changed" not in first
    assert [item["changed"] for item in later] == [
        ["i18n"],
        ["composer.contacts_note", "response.contact_hint"],
        ["response.contact_hint"],
        ["shell.judge_strip"],
    ]
    assert all(len(item["commit"]) == 40 and item["reason"] for item in later)
    answer = _by_label(RECORDED["responses"])["i18n"]
    strings = json.loads(answer["body"]["json"])["strings"]
    assert {key for key in strings if key.startswith("web.ui.login.")} == {
        f"web.ui.login.fact_{name}.{part}"
        for name in ("review", "tiers", "receipt")
        for part in ("title", "text")
    }


def test_make_app_without_public_has_no_public_settings():
    from corridor.web.app import make_app
    from tests.web.conftest import Doubles

    app = make_app(Doubles().settings(port=1))
    try:
        assert app.settings.get("public") is None
        assert app.settings.get("limits") is None
    finally:
        app.settings["executor"].shutdown()
    assert record_public_off.server_options() == RECORDED["server_options"]
    assert record_public_off.server_options()["xheaders"] is False
    assert record_public_off.route_table() == RECORDED["routes"]


def _by_label(responses):
    return {response["label"]: response for response in responses}


def test_without_public_the_wire_matches_the_golden():
    with serve({}) as app:
        current = record_public_off.record(app)
    expected = _by_label(RECORDED["responses"])
    actual = _by_label(current)
    assert list(actual) == list(expected)
    for label, want in expected.items():
        got = actual[label]
        assert (got["method"], got["path"], got["status"]) == (
            want["method"],
            want["path"],
            want["status"],
        ), label
        assert got["headers"] == want["headers"], label
        if "json" in want["body"]:
            assert json.loads(got["body"]["json"]) == json.loads(want["body"]["json"]), label
            assert got["body"]["json"] == want["body"]["json"], label
        else:
            assert got["body"] == want["body"], label


@pytest.mark.parametrize("label", ["open", "reuse", "other_open"])
def test_the_recorded_cookie_is_the_plain_one_without_secure(label):
    (cookie,) = [_by_label(RECORDED["responses"])[label]["headers"]["Set-Cookie"]]
    parts = [part.strip().lower() for part in cookie.split(";")]
    assert parts[0] == "corridor_web=<browser:1>" or parts[0] == "corridor_web=<browser:2>"
    assert {"httponly", "samesite=strict", "path=/"} <= set(parts)
    assert "secure" not in parts
    assert not any(part.startswith(("expires", "max-age", "domain")) for part in parts)
