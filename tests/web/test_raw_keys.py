"""No page shows a catalog key in place of its text (RW-9 item 3).

The login card showed ``web.ui.login.fact_review.title`` and five more on the first screen,
in every mode: the page's ``t`` falls back to the key for a missing string, and nothing
compared the keys the pages name with the catalog. Three guards now do.

- Here: every key the static files name is in ``/api/i18n``, and a key built at run time
  appears in ``DYNAMIC`` with every ending the code can produce.
- ``tests/web/conftest.py``: every JSON answer of every web test is scanned for a raw key
  (``raw_keys``), in both modes; the journeys below walk the pages for both.
- ``tests/web_e2e/specs/24-raw-keys.spec.cjs``: the rendered pages in a real browser, the
  first screen and the phone page included, in the scripted lane and the public lane.
"""

import io
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from tornado.httpclient import HTTPRequest, HTTPResponse
from tornado.httputil import HTTPHeaders

import corridor.web
from corridor.web import catalog
from corridor.web.public import PublicConfig
from corridor.web.security import HOST_COOKIE
from tests.web.conftest import RAW_KEY, WebClient, raw_keys, serve, ws_of
from tests.web.test_api_chat import Browser
from tests.web.test_tasks import RED, YELLOW

STATIC = Path(corridor.web.__file__).with_name("static")
HOST = "demo.example"
NAMED = re.compile(r"web\.(?:ui|vm)\.[A-Za-z0-9_.]*")
# Keys the pages build at run time: the prefix each script names, and every ending its code
# can produce. A new prefix fails ``test_every_key_the_pages_build_is_in_the_catalog`` until
# it is listed here.
DYNAMIC = {
    "web.ui.rogue.column.": ("category", "method", "rules", "verdict", "dispatch", "twin"),
    "web.ui.rogue.category.": (
        "wrong_recipient",
        "missing_mfa",
        "replayed_approval",
        "duplicate_dispatch",
        "sponsored_selection",
        "forged_receipt",
        "stopped_plan_dispatch",
        "sandbox_file",
    ),
    "web.ui.rogue.stat.": ("blocked", "dispatches", "controls"),
    "web.ui.rogue.attack.": (
        "wrong_recipient",
        "missing_mfa",
        "replayed_approval",
        "duplicate_dispatch",
        "sponsored_selection",
        "forged_receipt",
        "stopped_plan_dispatch",
        "sandbox_file",
    ),
    "web.ui.evidence.challenge.": ("none", "issued", "verified"),
    "web.ui.lab.check.": ("current_card", "withdrawn_request"),
    "web.ui.lab.prompt.": ("yellow", "red"),
    "web.ui.lab.prompt.changed.": ("yellow", "red"),
    "web.ui.lab.scenario.": ("yellow", "red"),
    "web.ui.lab.status.": (
        "refused",
        "done",
        "executed",
        "confirmed",
        "ok",
        "pending",
        "settled",
        "waiting_mfa",
        "unknown",
        "failed",
    ),
    "web.ui.lab.step.": ("prepare", "change", "try", "confirm"),
    "web.ui.tier.done.": ("green", "yellow", "red", "black"),
}
LOGIN_CARD = tuple(
    f"web.ui.login.fact_{name}.{part}"
    for name in ("review", "tiers", "receipt")
    for part in ("title", "text")
)
VIEWS = (("customer", "zh-CN"), ("customer", "en"), ("judge", "zh-CN"), ("judge", "en"))


def named_by_the_pages() -> set[str]:
    found: set[str] = set()
    for path in STATIC.rglob("*"):
        if path.suffix in (".js", ".html"):
            found.update(NAMED.findall(path.read_text(encoding="utf-8")))
    return found


def test_every_key_the_pages_name_is_in_the_catalog():
    exact = {key for key in named_by_the_pages() if not key.endswith(".")}
    assert exact, "the scan found nothing"
    assert exact - set(catalog.keys("web.ui.")) == set()


def test_every_key_the_pages_build_is_in_the_catalog():
    assert {key for key in named_by_the_pages() if key.endswith(".")} == set(DYNAMIC)
    known = set(catalog.keys("web.ui."))
    assert {f"{prefix}{end}" for prefix, ends in DYNAMIC.items() for end in ends} <= known


def test_the_login_card_has_its_six_strings_in_both_locales():
    for key in LOGIN_CARD:
        entry = catalog.WEB_MESSAGES[key]
        assert set(entry) == {"zh-CN", "en"}
        assert all(text.strip() and text != key for text in entry.values())
    assert set(LOGIN_CARD) <= set(catalog.keys("web.ui.login."))


def response_of(path: str, body: dict) -> HTTPResponse:
    headers = HTTPHeaders({"Content-Type": "application/json; charset=UTF-8"})
    return HTTPResponse(
        HTTPRequest("http://127.0.0.1" + path),
        200,
        headers=headers,
        buffer=io.BytesIO(json.dumps(body).encode("utf-8")),
    )


@pytest.mark.parametrize(
    "text",
    [
        "web.ui.login.fact_review.title",
        "web.vm.status.settled",
        "polish.phone.title",
        "see web.vm.fact.amount here",
    ],
)
def test_the_scan_finds_a_key_in_an_answer(text):
    assert raw_keys("/api/state", response_of("/api/state", {"state": {"text": text}}))


def test_the_scan_reads_the_catalogs_values_not_its_keys():
    fine = {"strings": {"web.ui.login.fact_review.title": "先核对，再确认"}}
    assert raw_keys("/api/i18n", response_of("/api/i18n", fine)) == []
    broken = {"strings": {"web.ui.login.fact_review.title": "web.ui.login.fact_review.title"}}
    assert raw_keys("/api/i18n?x=1", response_of("/api/i18n", broken))


def test_the_scan_leaves_ordinary_words_alone():
    for text in ("example.web.ui.x", "web.ui", "polish", "Polish.notes"):
        assert RAW_KEY.search(text) is None, text


def local_browser(app) -> WebClient:
    return WebClient(app)


def public_browser(app) -> WebClient:
    return WebClient(
        app, host=HOST, scheme="https", cookie_name=HOST_COOKIE, forwarded_for="203.0.113.5"
    )


MODES = {
    "local": SimpleNamespace(settings={}, browser=local_browser),
    "public": SimpleNamespace(
        settings={"public": PublicConfig(host=HOST), "clock": lambda: 0.0},
        browser=public_browser,
    ),
}


class Page(Browser):
    """The page's own requests, with the mode's browser."""

    def __init__(self, app, client, view, locale):
        self.app = app
        self.client = client
        self.bodies = []
        self.state = client.open(view=view, locale=locale)["state"]
        self._ids = 0


@pytest.mark.parametrize("mode", sorted(MODES))
@pytest.mark.parametrize(("view", "locale"), VIEWS)
def test_the_first_screen_and_the_catalog_show_no_key(st_sentinel, mode, view, locale):
    with serve(dict(MODES[mode].settings)) as app:
        page = Page(app, MODES[mode].browser(app), view, locale)
        assert page.state["login"]
        strings = page.get("/api/i18n")["strings"]
        assert {key for key in named_by_the_pages() if not key.endswith(".")} <= set(strings)
        assert all(strings[key] != key for key in LOGIN_CARD)
        for path in ("/", f"/phone/{ws_of(app, page.client).pair}"):
            shown = page.client.get(path).body.decode("utf-8")
            assert not RAW_KEY.search(shown), path


@pytest.mark.parametrize("mode", sorted(MODES))
@pytest.mark.parametrize(("view", "locale"), VIEWS)
def test_a_journey_through_the_pages_shows_no_key(st_sentinel, mode, view, locale):
    """Every answer here goes through the scan in ``WebClient.request``: the view models
    build keys at run time too, and ``wtr`` answers a missing one with the key."""
    with serve(dict(MODES[mode].settings)) as app:
        page = Page(app, MODES[mode].browser(app), view, locale)
        page.login()
        yellow = "T" + page.send(YELLOW)
        page.device(yellow, "confirm")
        red = "T" + page.send(RED)
        page.device(red, "confirm")
        page.device(red, "mfa_request")
        page.device(red, "mfa_verify", code=page.sms_code())
        page.device(red, "confirm")
        for name in page.state["capabilities"]["tabs"]:
            page.tab(name)
        page.client.get(f"/api/phone/{page.pair()}?lang={locale}&view={view}")
        page.get("/api/live")
        page.post("/api/session/reset", {})
