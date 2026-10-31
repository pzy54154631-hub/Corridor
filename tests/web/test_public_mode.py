"""Public mode, in its units and over the wire (design §3, §4.1–4.6, §4.12; RW-8).

The public fixtures serve ``make_app`` with a ``PublicConfig`` for ``demo.example`` and
drive it as the reverse proxy would: ``Host: demo.example``, ``Origin: https://demo.example``,
an ``X-Forwarded-For`` address and the ``__Host-`` cookie. The clock is injected, so idle
intervals and bucket refills are driven, never waited for. ``web_app`` and ``client`` in
this module are the same application without public mode, for every contrast.

The external review's paths each have a regression here: B1 (reset churn past the
outbox's bound), C1 (one source cannot fill the executor for everyone), C2 (a polling
phone keeps its session), C3 (a drained bucket survives a full table), and the public and
non-public fixtures side by side.
"""

import asyncio
import http.client
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from tornado.httputil import HTTPHeaders

from corridor.ui.phone_outbox import MAX_PAIRS, OUTBOX
from corridor.web import public as public_mode
from corridor.web.public import RATES, Inflight, Limits, PublicConfig, Rate, RateLimiter, client_ip
from corridor.web.security import HOST_COOKIE
from tests.web.conftest import SMS_CODE, WebClient, serve, ws_of
from tests.web.test_api_chat import Browser
from tests.web.test_tasks import RED

HOST = "demo.example"
ADDRESS = "203.0.113.5"
OTHER_ADDRESS = "198.51.100.7"
MESSAGE = {"text": "hi", "external_files": 0, "client_msg_id": "m-000001"}
DEEPSEEK = frozenset({"deepseek-flash"})
# Small buckets, so a test drains one in a dozen requests; the clock never moves by itself.
TIGHT = {
    **RATES,
    "session": Rate(3, 1000.0),
    "unlock": Rate(2, 1000.0),
    "api": Rate(12, 1000.0),
    "phone": Rate(4, 1000.0),
    "page": Rate(5, 1000.0),
}


class Clock:
    def __init__(self, at: float = 1000.0) -> None:
        self.at = at

    def __call__(self) -> float:
        return self.at

    def advance(self, seconds: float) -> None:
        self.at += seconds


def config(**overrides) -> PublicConfig:
    return PublicConfig(host=HOST, **overrides)


def browser(app, address: str = ADDRESS) -> WebClient:
    """A browser behind the proxy: the public Host, an https Origin, the forwarded address."""
    return WebClient(app, host=HOST, scheme="https", cookie_name=HOST_COOKIE, forwarded_for=address)


def error_of(client, response):
    return client.json(response)["error"]["code"]


def busy(client, response) -> None:
    """A refusal by a limit: 503 ``busy`` with a retry hint and ``Retry-After``, no state."""
    assert response.code == 503, (response.code, response.body[:200])
    data = client.json(response)
    assert data["ok"] is False and data["error"]["code"] == "busy" and data["error"]["retry"]
    assert "state" not in data and "generation" not in data
    assert response.headers["Retry-After"] == "5"
    assert not response.headers.get_list("Set-Cookie")


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def web_settings(doubles):
    return doubles.settings()


@pytest.fixture
def public_app(web_settings, clock):
    with serve({**web_settings, "public": config(), "clock": clock}) as app:
        yield app


@pytest.fixture
def public_client(public_app) -> WebClient:
    return browser(public_app)


@pytest.fixture
def tight_app(web_settings, clock):
    with serve({**web_settings, "public": config(rates=TIGHT), "clock": clock}) as app:
        yield app


@pytest.fixture
def wide_app(web_settings, clock):
    """Six workers, so one address's blocked sessions still leave a worker for another."""
    with serve({**web_settings, "public": config(), "clock": clock, "pool_main": 6}) as app:
        yield app


@pytest.fixture
def small_app(web_settings, clock):
    """Three sessions, two per address, reaped after 1000 s idle, evictable after 600 s."""
    settings = config(max_sessions=3, idle_seconds=1000, evict_idle_after=600, per_ip_sessions=2)
    with serve({**web_settings, "public": settings, "clock": clock}) as app:
        yield app


@pytest.fixture
def real_public_app(st_sentinel, clock):
    with serve({"public": config(), "clock": clock}) as app:
        yield app


# --- T-1: the units of public.py -------------------------------------------------------------


@pytest.mark.parametrize(
    ("lines", "expected"),
    [
        (["203.0.113.5"], "203.0.113.5"),
        (["10.0.0.1, 203.0.113.5"], "203.0.113.5"),
        (["10.0.0.1", "203.0.113.9"], "203.0.113.9"),
        (["2001:db8:1234:5678:abcd::1"], "2001:db8:1234:5678::/64"),
        (["2001:db8:1234:5678::1, 2001:db8:1234:5678:ffff::2"], "2001:db8:1234:5678::/64"),
        (["::ffff:203.0.113.5"], "203.0.113.5"),
        (["203.0.113.5:443"], "unknown"),
        (["[2001:db8::1]:443"], "unknown"),
        (["evil"], "unknown"),
        (["203.0.113.5,"], "unknown"),
        ([""], "unknown"),
        ([], "unknown"),
    ],
)
def test_client_ip_reads_the_right_most_forwarded_address(lines, expected):
    headers = HTTPHeaders()
    for line in lines:
        headers.add("X-Forwarded-For", line)
    assert client_ip(headers) == expected


def test_client_ip_never_reads_other_address_headers():
    headers = HTTPHeaders()
    headers.add("X-Real-Ip", "203.0.113.5")
    headers.add("Forwarded", "for=203.0.113.5")
    assert client_ip(headers) == "unknown"


def test_a_rate_limiter_drains_and_refills_by_its_clock():
    clock = Clock()
    limiter = RateLimiter({"api": Rate(3, 10.0)}, clock=clock)
    assert [limiter.allow("api", "a") for _ in range(4)] == [True, True, True, False]
    assert limiter.allow("api", "b")  # another key has its own bucket
    clock.advance(10)
    assert limiter.allow("api", "a") and not limiter.allow("api", "a")
    clock.advance(1000)
    assert [limiter.allow("api", "a") for _ in range(4)] == [True, True, True, False]
    assert limiter.refused == {"api": 3}
    assert limiter.drain_refused() == {"api": 3} and limiter.drain_refused() == {}


def test_a_drained_bucket_survives_a_full_table_and_new_keys_share_the_overflow():
    """RW-8 item 7 (C3): a bucket that has not refilled is never dropped to make room, so
    flooding the table with other keys does not hand the attacker a fresh bucket; while the
    table is full of such buckets every new key shares one overflow bucket."""
    clock = Clock()
    limiter = RateLimiter({"api": Rate(2, 10.0)}, table_size=3, clock=clock)
    assert limiter.allow("api", "attacker") and limiter.allow("api", "attacker")
    assert not limiter.allow("api", "attacker")
    assert limiter.allow("api", "b") and limiter.allow("api", "c")
    assert limiter.size("api") == 3
    # Nothing in the table has refilled: newcomers share the overflow bucket (2 tokens).
    assert limiter.allow("api", "d") and limiter.allow("api", "e")
    assert not limiter.allow("api", "f") and not limiter.allow("api", "d")
    assert limiter.size("api") == 3
    # Back to the attacker: still drained, not reset.
    assert not limiter.allow("api", "attacker")
    # After one interval b and c are full again and may go; the attacker's bucket stays
    # and has exactly the one token the interval gave back.
    clock.advance(10)
    assert limiter.allow("api", "g")
    assert limiter.size("api") == 2 and limiter.tokens("api", "attacker") is not None
    assert limiter.allow("api", "attacker") and not limiter.allow("api", "attacker")


def test_in_flight_places_are_counted_per_source_and_per_browser():
    inflight = Inflight(2, 1)
    first = inflight.acquire("ip", "b1")
    assert first is not None and inflight.acquire("ip", "b1") is None  # the browser's one
    second = inflight.acquire("ip", "b2")
    assert second is not None and inflight.acquire("ip", "b3") is None  # the source's two
    assert inflight.acquire("ip", None) is None
    elsewhere = inflight.acquire("other", None)
    assert elsewhere is not None
    inflight.release(first)
    assert inflight.acquire("ip", "b1") is not None
    for held in (second, elsewhere):
        inflight.release(held)
    assert inflight.sources == {"ip": 1} and inflight.browsers == {"b1": 1}


def namespace(**values):
    base = dict.fromkeys((
        "public_host",
        "max_sessions",
        "idle_seconds",
        "evict_idle_after",
        "per_ip_sessions",
        "per_ip_new_sessions",
    ))
    base.update(values)
    return SimpleNamespace(**base)


def test_without_a_public_host_there_is_no_public_config():
    assert PublicConfig.from_args(namespace(), {}) is None
    assert PublicConfig.from_args(namespace(public_host=""), {"OTHER": "x"}) is None


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("idle_seconds", 60.0),
        ("evict_idle_after", 60.0),
        ("per_ip_sessions", 4),
        ("per_ip_new_sessions", "30/600"),
    ],
)
def test_public_only_options_are_refused_without_a_public_host(name, value):
    with pytest.raises(ValueError, match="only apply with --public-host"):
        PublicConfig.from_args(namespace(**{name: value}), {})


def test_the_environment_opens_public_mode_with_the_rw8_capacities():
    settings = PublicConfig.from_args(
        namespace(), {public_mode.HOST_ENV: "43-154-247-126.sslip.io"}
    )
    assert settings is not None and settings.host == "43-154-247-126.sslip.io"
    assert settings.max_sessions == 128
    assert (settings.idle_seconds, settings.evict_idle_after) == (3600.0, 1800.0)
    assert settings.per_ip_sessions == 16 and settings.rates["session"] == Rate(30, 20.0)
    assert (settings.inflight_per_source, settings.inflight_per_browser) == (12, 3)
    assert settings.rates["page"] == Rate(1200, 1 / 60)
    assert settings.deployments == DEEPSEEK and settings.table_size == 4096
    assert set(settings.rates) == {"session", "unlock", "api", "phone", "page"}


@pytest.mark.parametrize(
    "host",
    [
        "DEMO.example",
        "demo.example:443",
        "demo.example.",
        "demo",
        "-demo.example",
        "demo_x.example",
        "https://demo.example",
        "demo.example/",
        "demo example",
        "127.0.0.1",
    ],
)
def test_a_public_host_is_a_lower_case_dns_name_without_port_or_path(host):
    with pytest.raises(ValueError, match="--public-host"):
        PublicConfig.from_args(namespace(public_host=host), {})


@pytest.mark.parametrize(
    "listed", ["", "zhipu-glm-5.3", "deepseek-flash,sol", "deepseek-flash,zhipu-glm-5.3", "sol"]
)
def test_public_mode_requires_exactly_the_deepseek_flash_deployment(listed):
    with pytest.raises(ValueError, match="RW-8 item 6"):
        PublicConfig.from_args(namespace(public_host=HOST), {public_mode.DEPLOYMENTS_ENV: listed})
    allowed = PublicConfig.from_args(
        namespace(public_host=HOST), {public_mode.DEPLOYMENTS_ENV: " deepseek-flash "}
    )
    assert allowed is not None and allowed.deployments == DEEPSEEK


def test_the_entry_options_set_the_capacities_and_the_new_session_rate():
    settings = PublicConfig.from_args(
        namespace(
            public_host=HOST,
            max_sessions=48,
            idle_seconds=1200.0,
            evict_idle_after=600.0,
            per_ip_sessions=8,
            per_ip_new_sessions="16/600",
        ),
        {},
    )
    assert (settings.max_sessions, settings.idle_seconds, settings.evict_idle_after) == (
        48,
        1200.0,
        600.0,
    )
    assert settings.per_ip_sessions == 8 and settings.rates["session"] == Rate(16, 37.5)
    assert settings.rates["api"] == RATES["api"]
    for bad in ("16", "16/0", "x/600", "16/600/1", "0/600", "/600"):
        with pytest.raises(ValueError):
            Rate.parse(bad)


@pytest.mark.parametrize(
    "overrides",
    [
        {"idle_seconds": 100, "evict_idle_after": 200},
        {"idle_seconds": 0},
        {"max_sessions": 0},
        {"per_ip_sessions": 0},
        {"inflight_per_source": 0},
        {"inflight_per_browser": 0},
        {"rates": {"api": Rate(1, 1.0)}},
        {"deployments": frozenset({"deepseek-flash", "sol"})},
        {"table_size": 0},
    ],
)
def test_an_impossible_public_config_is_refused(overrides):
    with pytest.raises(ValueError):
        config(**overrides)


def test_make_app_carries_the_config_and_the_limits_only_in_public_mode(doubles):
    from corridor.web.app import make_app

    plain = make_app(doubles.settings(port=1))
    public = make_app(doubles.settings(port=1, public=config(), clock=Clock()))
    try:
        assert plain.settings["public"] is None and plain.settings["limits"] is None
        assert plain.settings["store"].public is None
        assert isinstance(public.settings["limits"], Limits)
        assert public.settings["public"] is public.settings["store"].public
        assert public.settings["public"].host == HOST
    finally:
        for app in (plain, public):
            app.settings["executor"].shutdown()


# --- T-2: Host and Origin ----------------------------------------------------------------------


def test_the_public_host_over_https_is_served_for_reads_writes_pages_and_the_phone(
    public_app, public_client
):
    data = public_client.open(view="customer")
    assert data["ok"] and data["state"]["view"] == "customer"
    assert public_client.post("/api/chat/stop", {}).code == 200
    assert public_client.get("/api/state").code == 200
    assert public_client.get("/").code == 200
    assert public_client.get("/static/js/main.js").code == 200
    pair = ws_of(public_app, public_client).pair
    assert public_client.get(f"/api/phone/{pair}").code == 200
    assert public_client.get(f"/phone/{pair}").code == 200


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1:{port}",
        "localhost:{port}",
        "[::1]:{port}",
        "demo.example:443",
        "DEMO.example",
        "demo.example.",
        "evil.example",
        "demo.example.evil.example",
        "xn--demo.example",
        "",
    ],
)
def test_every_host_but_the_public_name_is_refused_in_public_mode(public_app, doubles, host):
    client = browser(public_app)
    host = host.format(port=client.port)
    headers = {"Host": host, "Origin": "https://" + host}
    response = client.post("/api/session", {}, headers=headers)
    assert response.code == 403 and error_of(client, response) == "forbidden_origin"
    assert not response.headers.get_list("Set-Cookie") and doubles.sessions == []
    assert client.get("/api/state", headers={"Host": host}).code == 403
    assert client.get("/", headers={"Host": host}).code == 403
    assert client.get("/static/js/main.js", headers={"Host": host}).code == 403


@pytest.mark.parametrize(
    "origin",
    [
        None,
        "null",
        "",
        "http://demo.example",
        "https://demo.example:443",
        "https://DEMO.example",
        "https://evil.example",
        "https://demo.example/",
        "https://demo.example.evil.example",
        ["https://demo.example", "https://demo.example"],
    ],
)
def test_writes_need_exactly_the_https_origin_of_the_public_name(public_app, doubles, origin):
    client = browser(public_app)
    response = client.post("/api/session", {}, headers={"Origin": origin})
    assert response.code == 403 and error_of(client, response) == "forbidden_origin"
    assert not response.headers.get_list("Set-Cookie") and doubles.sessions == []


def test_reads_refuse_a_foreign_origin_and_cross_site_fetches_in_public_mode(
    public_app, public_client
):
    public_client.open()
    ws = ws_of(public_app, public_client)
    for headers in (
        {"Origin": "https://evil.example"},
        {"Origin": "http://demo.example"},
        {"Sec-Fetch-Site": "cross-site"},
        {"Sec-Fetch-Site": "same-site"},
    ):
        assert public_client.get("/api/state", headers=headers).code == 403, headers
        assert public_client.post("/api/chat/stop", {}, headers=headers).code == 403, headers
    assert ws.revision == 0


# --- T-3: the cookie, the buckets, the vendor script ------------------------------------------


def test_the_public_cookie_is_host_prefixed_secure_and_session_scoped(public_client):
    response = public_client.post("/api/session", {})
    (cookie,) = response.headers.get_list("Set-Cookie")
    parts = [part.strip().lower() for part in cookie.split(";")]
    assert parts[0].startswith("__host-corridor_web=")
    assert {"secure", "httponly", "samesite=strict", "path=/"} <= set(parts)
    assert not any(part.startswith(("expires", "max-age", "domain")) for part in parts)
    assert public_client.browser is not None
    assert public_client.post("/api/chat/stop", {}).code == 200


def test_the_plain_cookie_is_not_this_services_in_public_mode(client, public_client):
    plain = client.post("/api/session", {})
    (cookie,) = plain.headers.get_list("Set-Cookie")
    assert cookie.startswith("corridor_web=") and "secure" not in cookie.lower()
    public_client.open()
    unprefixed = {"Cookie": f"corridor_web={public_client.browser}"}
    response = public_client.get("/api/state", headers=unprefixed)
    assert response.code == 401 and error_of(public_client, response) == "no_session"
    both = {"Cookie": f"corridor_web=x; {HOST_COOKIE}={public_client.browser}"}
    assert public_client.get("/api/state", headers=both).code == 200
    doubled = {"Cookie": f"{HOST_COOKIE}=x; {HOST_COOKIE}={public_client.browser}"}
    assert public_client.get("/api/state", headers=doubled).code == 403


def test_new_sessions_per_address_are_limited_and_reuse_is_not(tight_app, doubles, clock):
    clients = [browser(tight_app) for _ in range(4)]
    for client in clients[:3]:
        client.open()
    busy(clients[3], clients[3].post("/api/session", {}))
    assert len(doubles.sessions) == 3
    # Reusing a session draws nothing from the address's session bucket.
    for _ in range(3):
        assert clients[0].post("/api/session", {"tab": clients[0].tab}).code == 200
    assert len(doubles.sessions) == 3
    # Another address has its own bucket; a token comes back with the clock.
    assert browser(tight_app, OTHER_ADDRESS).post("/api/session", {}).code == 200
    busy(clients[3], clients[3].post("/api/session", {}))
    clock.advance(1000.0)  # one token back
    assert clients[3].post("/api/session", {}).code == 200
    busy(clients[3], clients[3].post("/api/session", {}))


def test_api_calls_per_address_are_limited_before_the_session_is_touched(tight_app, doubles):
    client = browser(tight_app)
    client.open()  # one call from the api bucket
    ws = ws_of(tight_app, client)
    before = len(doubles.names(ws))
    answers = [client.get("/api/state") for _ in range(12)]
    assert [answer.code for answer in answers] == [200] * 11 + [503]
    busy(client, answers[-1])
    assert len(doubles.names(ws)) == before + 11 and ws.revision == 0
    # The phone and the pages draw from buckets of their own.
    assert client.get(f"/api/phone/{ws.pair}").code == 200
    assert client.get("/").code == 200
    # Another address is not affected.
    other = browser(tight_app, OTHER_ADDRESS)
    other.open()
    assert other.get("/api/state").code == 200


def test_phone_polls_draw_from_their_own_bucket(tight_app):
    client = browser(tight_app)
    client.open()
    pair = ws_of(tight_app, client).pair
    answers = [client.get(f"/api/phone/{pair}") for _ in range(5)]
    assert [answer.code for answer in answers] == [200] * 4 + [503]
    busy(client, answers[-1])
    assert client.get("/api/state").code == 200


def test_unlock_attempts_per_address_are_limited_before_the_live_adapter(tight_app, doubles):
    client = browser(tight_app)
    client.open()
    ws = ws_of(tight_app, client)
    body = {"nonce": "0" * 32, "proof": "0" * 64}
    answers = [client.post("/api/live/unlock", body) for _ in range(3)]
    assert [answer.code for answer in answers] == [200, 200, 503]
    busy(client, answers[-1])
    assert doubles.names(ws).count("live.unlock") == 2
    # Other live routes and the API in general still answer: only the unlock bucket is dry.
    assert client.post("/api/live/challenge", {}).code == 200


def test_pages_and_static_files_per_address_are_limited(tight_app):
    client = browser(tight_app)
    answers = [client.get("/static/js/main.js") for _ in range(6)]
    assert [answer.code for answer in answers] == [200] * 5 + [503]
    busy(client, answers[-1])
    assert client.get("/").code == 503
    assert browser(tight_app, OTHER_ADDRESS).get("/").code == 200


def test_a_request_without_a_usable_address_shares_the_unknown_bucket(tight_app):
    missing = browser(tight_app, None)
    malformed = WebClient(
        tight_app, host=HOST, scheme="https", cookie_name=HOST_COOKIE, forwarded_for="not-an-ip"
    )
    assert missing.get("/").code == 200 and malformed.get("/").code == 200
    for _ in range(3):
        assert missing.get("/").code == 200
    busy(malformed, malformed.get("/"))
    busy(missing, missing.get("/"))
    assert tight_app.settings["limits"].limiter.tokens("page", "unknown") == 0


def test_the_closed_lab_answers_404_before_the_transport_check_and_the_limits(tight_app):
    client = browser(tight_app)
    client.open()
    limiter = tight_app.settings["limits"].limiter
    before = (limiter.tokens("api", ADDRESS), limiter.tokens("page", ADDRESS))
    for path in ("/api/lab", "/api/lab/start", "/api/lab/anything/below"):
        assert client.get(path).code == 404
        assert client.post(path, {}).code == 404
        assert client.get(path, headers={"Host": "evil.example"}).code == 404
    assert (limiter.tokens("api", ADDRESS), limiter.tokens("page", ADDRESS)) == before


def test_a_transport_refusal_comes_before_the_buckets(tight_app):
    client = browser(tight_app)
    limiter = tight_app.settings["limits"].limiter
    for _ in range(3):
        assert client.get("/", headers={"Host": "evil.example"}).code == 403
    assert limiter.tokens("page", ADDRESS) is None


def test_the_vendor_script_is_no_cache_only_in_public_mode(public_client, client):
    public = public_client.get("/vendor/corridor-hmac.js")
    assert public.code == 200 and public.headers["Cache-Control"] == "no-cache"
    plain = client.get("/vendor/corridor-hmac.js")
    assert plain.code == 200 and "Cache-Control" not in plain.headers


def test_sessions_carry_the_public_flags_from_creation_reuse_and_reset(
    public_app, public_client, client
):
    """RW-8 item 5 (B3): the flags are set when the session is built or rebuilt, before
    any state is rendered, so the first response, a reuse and a reset all see them."""
    public_client.open()
    ws = ws_of(public_app, public_client)
    assert ws.public is True and ws.public_deployments == DEEPSEEK
    public_client.post("/api/session", {"tab": public_client.tab})
    assert ws_of(public_app, public_client) is ws and ws.public is True
    public_client.post("/api/session/reset", {})
    renewed = ws_of(public_app, public_client)
    assert renewed is not ws and renewed.public is True
    assert renewed.public_deployments == DEEPSEEK
    client.open()
    plain = ws_of(client.app, client)
    assert plain.public is False and plain.public_deployments is None


# --- RW-8 item 2 (C1): the shared executor -------------------------------------------------------


def test_one_address_cannot_fill_the_executor_for_everyone(wide_app, doubles):
    """Four browsers at one address hold three requests each inside or ahead of a blocked
    service call; a fourth from any browser and a thirteenth from the address are refused
    at once, taking no queue place, while another address completes its operation."""
    public_app = wide_app
    gate = doubles.block("send_message")
    flood = [browser(public_app) for _ in range(4)]
    for client in flood:
        client.open()
    answers = {}
    threads = []

    def send(index, client):
        answers[index] = client.post("/api/chat/message", MESSAGE)

    for index, client in enumerate(flood * 3):
        thread = threading.Thread(target=send, args=(index, client))
        thread.start()
        threads.append(thread)
    assert doubles.wait_entered("send_message", 4)
    deadline = time.monotonic() + 10
    while public_app.settings["limits"].inflight.sources.get(ADDRESS) != 12:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    lanes = public_app.settings["executor"].lanes
    assert lanes["main"].admitted == 12
    try:
        busy(flood[0], flood[0].post("/api/chat/message", MESSAGE))  # the browser's fourth
        busy(flood[1], flood[1].get("/api/state"))  # a read counts the same
        third = browser(public_app)
        busy(third, third.post("/api/session", {}))  # the address's thirteenth
        assert lanes["main"].admitted == 12 and len(doubles.sessions) == 4
        other = browser(public_app, OTHER_ADDRESS)
        other.open()
        assert other.post("/api/chat/stop", {}).code == 200
        assert other.get("/api/state").code == 200
    finally:
        gate.set()
        for thread in threads:
            thread.join(10)
    assert sorted(answer.code for answer in answers.values()) == [200] * 12
    assert public_app.settings["limits"].inflight.sources == {}
    assert public_app.settings["limits"].inflight.browsers == {}
    assert lanes["main"].admitted == 0


def test_ten_phones_behind_one_address_poll_for_as_long_as_they_like():
    """RW-9 item 5: each phone polls once a second (``static/js/views/sms.js``). Four tokens
    back a second ran the burst dry in twenty seconds, then six polls in ten were refused."""
    assert 1 / RATES["phone"].interval >= 12
    clock = Clock()
    limiter = RateLimiter(RATES, clock=clock)
    for _ in range(3600):
        clock.advance(1.0)
        assert all(limiter.allow("phone", ADDRESS) for _ in range(10))
    assert limiter.refused == {}


def test_the_old_refill_would_have_refused_those_phones():
    clock = Clock()
    limiter = RateLimiter({**RATES, "phone": Rate(120, 0.25)}, clock=clock)
    allowed = []
    for _ in range(60):
        clock.advance(1.0)
        allowed.append(sum(limiter.allow("phone", ADDRESS) for _ in range(10)))
    assert allowed[0] == 10 and allowed[-1] == 4


def test_ten_phones_poll_over_the_wire_while_the_clock_runs(web_settings, clock):
    with serve({**web_settings, "public": config(), "clock": clock}) as app:
        phones = [browser(app) for _ in range(10)]
        for phone in phones:
            phone.open(view="customer")
        pairs = [ws_of(app, phone).pair for phone in phones]
        for _ in range(40):
            clock.advance(1.0)
            for phone, pair in zip(phones, pairs, strict=True):
                answer = phone.get(f"/api/phone/{pair}?lang=zh-CN&view=customer")
                assert answer.code == 200, answer.body[:200]


def page_files() -> list[str]:
    """What a browser can fetch for the first screen: the page, every stylesheet and module."""
    static = Path(public_mode.__file__).with_name("static")
    names = (path.relative_to(static).as_posix() for path in static.rglob("*"))
    return ["/", *(f"/static/{name}" for name in sorted(names) if name.endswith((".css", ".js")))]


def test_ten_browsers_behind_one_address_open_log_in_and_work_at_once(public_app, doubles):
    """RW-10 item 2: judges at one venue share one exit. With the clock standing still, so
    nothing refills, ten browsers load the page, open, log in and send one message each,
    and all ten messages are in flight together. Nothing is refused."""
    clients = [browser(public_app) for _ in range(10)]
    for client in clients:
        for path in page_files():
            assert client.get(path).code == 200, path
    for client in clients:
        client.open()
        assert client.post("/api/session/login", {}).code == 200
    gate = doubles.block("send_message")
    answers = {}

    def send(index, client):
        answers[index] = client.post("/api/chat/message", MESSAGE)

    threads = [threading.Thread(target=send, args=pair) for pair in enumerate(clients)]
    limits = public_app.settings["limits"]
    lane = public_app.settings["executor"].lanes["main"]
    for thread in threads:
        thread.start()
    try:
        assert doubles.wait_entered("send_message", 4)  # the pool's four workers
        deadline = time.monotonic() + 10
        while limits.inflight.sources.get(ADDRESS) != 10:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert lane.admitted == 10 and lane.limit > limits.config.inflight_per_source
    finally:
        gate.set()
        for thread in threads:
            thread.join(10)
    assert [answers[index].code for index in range(10)] == [200] * 10
    assert limits.limiter.refused == {}


def test_the_old_limits_would_have_stopped_those_browsers():
    """DEPLOY-1 on the target: none of ten browsers reached the first screen."""
    wanted = 10 * len(page_files())
    limiter = RateLimiter({**RATES, "page": Rate(300, 0.2)}, clock=Clock())
    assert sum(limiter.allow("page", ADDRESS) for _ in range(wanted)) == 300 < wanted
    inflight = Inflight(6, 3)
    held = [inflight.acquire(ADDRESS, f"browser-{index}") for index in range(10)]
    assert sum(place is not None for place in held) == 6


def test_a_refused_request_releases_its_place_and_the_browser_goes_on(public_app, doubles):
    public_client = browser(public_app)
    public_client.open()
    inflight = public_app.settings["limits"].inflight
    for path, body in (("/api/chat/stop", {}), ("/api/nothing", None), ("/api/lab", None)):
        response = public_client.post(path, body) if body is not None else public_client.get(path)
        assert response.code in (200, 404)
    assert public_client.post("/api/chat/message", {**MESSAGE, "text": " "}).code == 422
    assert public_client.post("/api/chat/stop", {}, headers={"Cookie": None}).code == 401
    assert inflight.sources == {} and inflight.browsers == {}


# --- T-4: the session table -------------------------------------------------------------------


def test_a_full_table_refuses_a_newcomer_rather_than_evicting_a_session_in_use(
    small_app, doubles, clock
):
    """RW-8 item 1: with every session used within ``evict_idle_after`` the newcomer gets
    503 and nobody is evicted; once one has been idle past the threshold, that one goes."""
    first, second, third, fourth = (
        browser(small_app, f"203.0.113.{index}") for index in range(1, 5)
    )
    for client in (first, second, third):
        client.open()
    busy(fourth, fourth.post("/api/session", {}))
    assert len(doubles.sessions) == 3 and "live.close" not in doubles.names()
    assert all(ws_of(small_app, client) is not None for client in (first, second, third))
    clock.advance(600)
    second.get("/api/state")
    third.post("/api/chat/stop", {})
    fourth.open()
    assert ws_of(small_app, first) is None
    assert all(ws_of(small_app, client) is not None for client in (second, third, fourth))
    response = first.post("/api/chat/stop", {})
    assert response.code == 401 and error_of(first, response) == "no_session"
    closed = [record.ws for record in doubles.records if record.name == "live.close"]
    assert len(closed) == 1 and closed[0].browser_id == first.browser
    assert not small_app.settings["store"].pairs.allowed(closed[0].pair)
    assert doubles.names(closed[0])[-3:] == ["retire_pair", "forget_pair", "live.close"]


def test_a_refusal_by_the_full_table_leaves_the_address_its_share(small_app, doubles, clock):
    """A newcomer refused because the table is full must not keep a reservation on its
    address: refused twice, it still opens once a session has gone idle past the threshold."""
    first, second, third, fourth = (
        browser(small_app, f"203.0.113.{index}") for index in range(1, 5)
    )
    for client in (first, second, third):
        client.open()
    for _ in range(2):
        busy(fourth, fourth.post("/api/session", {}))
    store = small_app.settings["store"]
    assert store._client_reserved == {} and store._reserved == {}
    clock.advance(600)
    fourth.open()
    assert ws_of(small_app, fourth) is not None and len(store) == 3
    assert store._clients == {"203.0.113.2": 1, "203.0.113.3": 1, "203.0.113.4": 1}


def test_a_browser_past_its_tabs_loses_its_own_idle_tab_as_before(public_app, doubles):
    """The eviction threshold guards other people's sessions; a browser opening a fifth
    tab still displaces its own least recently used tab, as the local service does."""
    tabs = [browser(public_app) for _ in range(5)]
    tabs[0].open()
    for tab in tabs[1:]:
        tab.browser = tabs[0].browser
        tab.open()
    store = public_app.settings["store"]
    assert len(store) == 4 and ws_of(public_app, tabs[0]) is None
    assert all(ws_of(public_app, tab) is not None for tab in tabs[1:])
    assert store._clients == {ADDRESS: 4}


def test_idle_sessions_are_reaped_and_busy_or_recent_ones_are_not(small_app, doubles, clock):
    idle, recent, working = (browser(small_app, f"203.0.113.{index}") for index in range(1, 4))
    for client in (idle, recent, working):
        client.open()
    store = small_app.settings["store"]
    gate = doubles.block("stop_all")
    answers = []
    thread = threading.Thread(target=lambda: answers.append(working.post("/api/stop", {})))
    thread.start()
    assert doubles.wait_entered("stop_all")
    clock.advance(500)
    recent.get("/api/state")
    clock.advance(500)
    try:
        assert public_mode.tick(small_app) == 1
        assert ws_of(small_app, idle) is None
        assert ws_of(small_app, recent) is not None and ws_of(small_app, working) is not None
    finally:
        gate.set()
        thread.join(10)
    assert answers[0].code == 200
    closed = [record.ws for record in doubles.records if record.name == "live.close"]
    assert [ws.browser_id for ws in closed] == [idle.browser]
    assert not store.pairs.allowed(closed[0].pair)
    assert doubles.names(closed[0])[-3:] == ["retire_pair", "forget_pair", "live.close"]
    # The reaped session's next request is 401; the page opens again, under a new generation.
    old = idle.generation
    response = idle.post("/api/chat/stop", {})
    assert response.code == 401 and error_of(idle, response) == "no_session"
    idle.open()
    assert idle.generation != old and ws_of(small_app, idle) is not None
    # Nothing is reaped before its time; then everything idle goes in one pass.
    working.get("/api/state")
    assert store.reap() == 0
    clock.advance(1000)
    assert store.reap() == 3 and len(store) == 0


def test_a_polling_phone_keeps_its_session_in_use(small_app, doubles, clock):
    """RW-8 item 3 (C2): the activity lease. A judge reading the simulated phone has the
    phone poll the inbox once a second; those polls, with a pairing still in the table,
    count as uses of the session, so neither the reaper nor a full table takes it."""
    watcher, other = browser(small_app, "203.0.113.1"), browser(small_app, "203.0.113.2")
    watcher.open()
    other.open()
    store = small_app.settings["store"]
    pair = ws_of(small_app, watcher).pair
    phone = browser(small_app, "198.51.100.9")  # the phone: no cookie, no tab
    for _ in range(3):
        clock.advance(400)
        assert phone.json(phone.get(f"/api/phone/{pair}"))["unpaired"] is None
    assert store.reap() == 1
    assert ws_of(small_app, watcher) is not None and ws_of(small_app, other) is None
    # A full table does not evict the watched session either: it is in use.
    newcomer, another, fourth = (browser(small_app, f"203.0.113.{index}") for index in (3, 4, 6))
    newcomer.open()
    clock.advance(400)
    phone.get(f"/api/phone/{pair}")
    another.open()  # the table is full: watcher, newcomer, another
    busy(fourth, fourth.post("/api/session", {}))  # nobody idle past the threshold
    clock.advance(600)
    phone.get(f"/api/phone/{pair}")
    fourth.open()  # the newcomer (idle 1000 s) goes, not the watched session
    assert ws_of(small_app, watcher) is not None and ws_of(small_app, newcomer) is None
    assert ws_of(small_app, another) is not None and len(store) == 3
    # Polls of a revoked or unknown pairing refresh nothing.
    assert watcher.post("/api/session/reset", {}).code == 200
    clock.advance(1000)
    assert phone.json(phone.get(f"/api/phone/{pair}"))["unpaired"]
    assert phone.get("/api/phone/" + "Z" * 22).code == 200
    assert store.reap() == 3 and ws_of(small_app, watcher) is None


def test_an_address_holds_at_most_its_share_of_sessions(small_app, doubles, clock):
    """RW-8 items 1 and 9: sessions are counted by the address that created them, the
    count is kept across a reset and given back when the session leaves the table."""
    same = [browser(small_app) for _ in range(3)]
    same[0].open()
    same[1].open()
    busy(same[2], same[2].post("/api/session", {}))
    assert len(doubles.sessions) == 2 and len(small_app.settings["store"]) == 2
    elsewhere = browser(small_app, OTHER_ADDRESS)
    elsewhere.open()
    assert len(small_app.settings["store"]) == 3
    assert same[0].post("/api/session/reset", {}).code == 200
    busy(same[2], same[2].post("/api/session", {}))
    # The address changing later does not move the session: it still counts at home.
    same[0].forwarded_for = OTHER_ADDRESS
    assert same[0].get("/api/state").code == 200
    busy(same[2], same[2].post("/api/session", {}))
    # Once one of the address's sessions is reaped, the address may open another.
    clock.advance(1000)
    same[1].get("/api/state")
    elsewhere.get("/api/state")
    assert small_app.settings["store"].reap() == 1
    assert same[2].post("/api/session", {}).code == 200
    assert len(doubles.sessions) == 5


def test_a_failing_session_build_gives_the_address_its_reservation_back(small_app, doubles):
    client = browser(small_app)
    original = doubles.refresh.refresh

    def failing(ws):
        raise RuntimeError("build")

    doubles.refresh.refresh = failing
    try:
        assert client.post("/api/session", {}).code == 500
    finally:
        doubles.refresh.refresh = original
    store = small_app.settings["store"]
    assert store._client_reserved == {} and store._clients == {ADDRESS: 1}
    assert store._reserved == {}


# --- RW-8 item 4 (B1, T-12): the outbox under churn ---------------------------------------------


def test_pairs_survive_reset_churn_beyond_max_pairs(real_public_app):
    """A keeps using its session while B resets more than ``MAX_PAIRS`` times; A's phone
    is still paired and every retired pairing has left the outbox. On 2273589 this fails:
    the outbox dropped A's inbox when the 65th pairing arrived."""
    keeper = browser(real_public_app, "203.0.113.1")
    churner = browser(real_public_app, "203.0.113.2")
    keeper.open(view="customer")
    churner.open(view="customer")
    pair = ws_of(real_public_app, keeper).pair
    retired = []
    for _ in range(MAX_PAIRS + 2):
        retired.append(ws_of(real_public_app, churner).pair)
        assert churner.post("/api/session/reset", {}).code == 200
    assert OUTBOX.known(pair)
    data = keeper.json(keeper.get(f"/api/phone/{pair}?lang=zh-CN&view=customer"))
    assert data["unpaired"] is None and data["messages"] == []
    assert not any(OUTBOX.known(old) for old in retired)
    assert OUTBOX.known(ws_of(real_public_app, churner).pair)


class PublicPage(Browser):
    """The page's own requests, from a browser behind the proxy at ``address``."""

    def __init__(self, app, address):
        self.app = app
        self.client = browser(app, address)
        self.bodies = []
        self.state = self.client.open(view="customer")["state"]
        self._ids = 0


def full_table(app, *, skip):
    """Every other session of the table, from as many addresses as their share takes."""
    public = app.settings["public"]
    return [
        PublicPage(app, f"198.51.100.{1 + index // public.per_ip_sessions}")
        for index in range(public.max_sessions - skip)
    ]


def test_a_full_table_keeps_the_first_sessions_inbox_and_its_code(real_public_app):
    """RW-9 item 1, the R8 probe's full table: 128 sessions, all in use. The session that
    came first still has its phone and the code on it still works. Before the fix the
    outbox dropped this inbox when the 65th session opened."""
    first = PublicPage(real_public_app, "203.0.113.1")
    first.login()
    task = "T" + first.send(RED)
    first.device(task, "confirm")
    first.device(task, "mfa_request")
    code = first.sms_code()
    assert code is not None and first.task(task)["step"] == "mfa_code"
    others = full_table(real_public_app, skip=1)
    assert len(real_public_app.settings["store"]) == config().max_sessions
    newcomer = browser(real_public_app, "192.0.2.200")
    busy(newcomer, newcomer.post("/api/session", {}))
    assert first.sms_code() == code
    assert all(OUTBOX.known(page.pair()) for page in others)
    first.device(task, "mfa_verify", code=code)
    assert first.task(task)["step"] == "final_confirm"


def test_a_reset_on_a_full_table_leaves_every_other_inbox_in_place(real_public_app):
    """A reset's new pairing exists beside the old one for a moment; with exactly one
    pairing per slot in the outbox, that moment would drop the oldest session's."""
    first = PublicPage(real_public_app, "203.0.113.1")
    others = full_table(real_public_app, skip=1)
    pairs = [page.pair() for page in others]
    for page in others[:8]:
        assert page.client.post("/api/session/reset", {}).code == 200
    assert OUTBOX.known(first.pair())
    assert all(OUTBOX.known(pair) for pair in pairs[8:])
    assert not any(OUTBOX.known(pair) for pair in pairs[:8])


def test_only_public_mode_raises_the_outbox_bound():
    """RW-9 item 1. A process each: the outbox is one per process, and public tests in this
    one have raised it already."""
    script = (
        "import sys\n"
        "from corridor.ui.phone_outbox import OUTBOX\n"
        "from corridor.web.app import make_app\n"
        "from corridor.web.public import PublicConfig\n"
        "public = PublicConfig(host='demo.example') if sys.argv[1] == 'public' else None\n"
        "app = make_app({'port': 1, 'public': public})\n"
        "print(OUTBOX.max_pairs)\n"
        "app.settings['executor'].shutdown()\n"
    )
    environ = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
    answers = [
        subprocess.run(
            [sys.executable, "-c", script, mode],
            capture_output=True,
            text=True,
            env=environ,
            timeout=120,
            check=True,
        ).stdout.strip()
        for mode in ("local", "public")
    ]
    assert answers == ["64", "256"]


def test_the_session_table_sizes_the_outbox_only_in_public_mode(doubles, clock):
    def asked():
        return [record.args for record in doubles.records if record.name == "reserve_pairs"]

    with serve(doubles.settings()):
        assert asked() == []
    with serve({**doubles.settings(), "public": config(), "clock": clock}):
        assert asked() == [(2 * config().max_sessions,)]
    with serve({**doubles.settings(), "public": config(), "clock": clock, "max_sessions": 200}):
        assert asked()[-1] == (400,)


def test_retired_pairings_leave_the_outbox_in_public_mode_and_stay_outside_it(
    real_public_app, st_sentinel
):
    client = browser(real_public_app)
    client.open(view="customer")
    old = ws_of(real_public_app, client).pair
    assert OUTBOX.known(old)
    assert client.post("/api/session/reset", {}).code == 200
    assert not OUTBOX.known(old) and OUTBOX.known(ws_of(real_public_app, client).pair)
    with serve({}) as local:
        plain = WebClient(local)
        plain.open(view="customer")
        old = ws_of(local, plain).pair
        assert plain.post("/api/session/reset", {}).code == 200
        assert OUTBOX.known(old)  # the local service is what it was: the inbox stays


def test_a_reset_whose_slot_was_taken_forgets_the_new_pairing_too(public_app, doubles):
    """Every reset branch cleans up: when the slot was emptied meanwhile the new session is
    retired at once, its pairing revoked, its codes expired and its inbox forgotten."""
    client = browser(public_app)
    client.open()
    store = public_app.settings["store"]
    ws = ws_of(public_app, client)
    store.close(ws)
    with pytest.raises(Exception) as refused:
        store.reset(ws)
    assert getattr(refused.value, "code", None) == "no_session"
    orphan = doubles.sessions[-1]
    assert orphan is not ws and not store.pairs.allowed(orphan.pair)
    assert doubles.names(orphan)[-4:] == ["live.on_new", "retire_pair", "forget_pair", "live.close"]
    assert doubles.names(ws)[-3:] == ["retire_pair", "forget_pair", "live.close"]


def failing_on_new(doubles):
    def refuse(ws):
        doubles.record("live.on_new", ws)
        raise RuntimeError("build")

    return refuse


def test_a_reset_that_fails_in_its_build_takes_the_new_pairing_back(public_app, doubles):
    """RW-9 item 6: the new session never reaches the table, so nothing else would ever
    revoke its pairing. The old session goes on as it was."""
    client = browser(public_app)
    client.open()
    store = public_app.settings["store"]
    ws = ws_of(public_app, client)
    doubles.live.on_new, original = failing_on_new(doubles), doubles.live.on_new
    try:
        assert client.post("/api/session/reset", {}).code >= 500
    finally:
        doubles.live.on_new = original
    orphan = doubles.sessions[-1]
    assert orphan is not ws
    assert not store.pairs.allowed(orphan.pair)
    assert doubles.names(orphan)[-3:] == ["retire_pair", "forget_pair", "live.close"]
    assert store.pairs.allowed(ws.pair) and ws_of(public_app, client) is ws
    assert client.post("/api/session/reset", {}).code == 200


def test_a_new_session_that_fails_in_its_build_takes_its_pairing_back(public_app, doubles):
    client = browser(public_app)
    store = public_app.settings["store"]
    doubles.live.on_new, original = failing_on_new(doubles), doubles.live.on_new
    try:
        assert client.post("/api/session", {}).code >= 500
    finally:
        doubles.live.on_new = original
    orphan = doubles.sessions[-1]
    assert not store.pairs.allowed(orphan.pair)
    assert doubles.names(orphan)[-3:] == ["retire_pair", "forget_pair", "live.close"]
    assert len(store) == 0 and store._reserved == {} and store._client_reserved == {}


def test_a_failed_reset_build_leaves_no_inbox_in_public_mode(real_public_app, monkeypatch):
    from corridor.web import live

    client = browser(real_public_app)
    client.open(view="customer")
    ws = ws_of(real_public_app, client)
    before = set(OUTBOX._pairs)

    def refuse(session):
        raise RuntimeError("build")

    monkeypatch.setattr(live, "on_new", refuse)
    assert client.post("/api/session/reset", {}).code >= 500
    monkeypatch.undo()
    assert not set(OUTBOX._pairs) - before and OUTBOX.known(ws.pair)
    assert client.post("/api/session/reset", {}).code == 200


def test_a_session_that_fails_to_build_registers_no_inbox(monkeypatch):
    from corridor.web import compat, state

    def refuse(*args, **kwargs):
        raise RuntimeError("build")

    before = set(OUTBOX._pairs)
    monkeypatch.setattr(compat, "DomesticSession", refuse)
    with pytest.raises(RuntimeError):
        state.new_web_session("customer", "zh-CN", browser_id="b", tab_id="t")
    assert not set(OUTBOX._pairs) - before


def test_a_failing_inbox_cleanup_is_logged_by_class_and_the_reset_still_answers(
    public_app, public_client, doubles, caplog
):
    caplog.set_level(logging.ERROR, logger="tornado.application")
    public_client.open()
    pair = ws_of(public_app, public_client).pair

    def broken(ws):
        raise RuntimeError("inbox-secret-" + ws.pair)

    doubles.state.forget_pair = broken
    response = public_client.post("/api/session/reset", {})
    assert response.code == 200
    assert "session cleanup failed: RuntimeError" in caplog.text
    assert "inbox-secret" not in caplog.text and pair not in caplog.text


# --- RW-8 item 13 (A2): whoever holds a valid pairing may read it ------------------------------


def test_whoever_holds_a_valid_pairing_may_read_it_and_nobody_a_revoked_one(
    public_app, public_client
):
    public_client.open()
    pair = ws_of(public_app, public_client).pair
    stranger = browser(public_app, OTHER_ADDRESS)  # no cookie, no tab, another address
    data = stranger.json(stranger.get(f"/api/phone/{pair}"))
    assert data["unpaired"] is None and SMS_CODE in json.dumps(data)
    public_client.post("/api/session/reset", {})
    revoked = stranger.json(stranger.get(f"/api/phone/{pair}"))
    unknown = stranger.json(stranger.get("/api/phone/" + "Z" * 22))
    assert revoked["unpaired"] and SMS_CODE not in json.dumps(revoked) and revoked == unknown


# --- T-11: the logs -------------------------------------------------------------------------------


def test_public_logs_hold_no_identifiers(tight_app, doubles, clock, caplog):
    for name in ("tornado.access", "tornado.application"):
        caplog.set_level(logging.DEBUG, logger=name)
    client = browser(tight_app)
    client.open()
    ws = ws_of(tight_app, client)
    pair, tab, generation, cookie = ws.pair, client.tab, client.generation, client.browser
    for _ in range(12):
        client.get("/api/state")
    for _ in range(5):
        client.get(f"/api/phone/{pair}")
    clock.advance(10_000)
    assert public_mode.tick(tight_app) == 1
    deadline = time.monotonic() + 10
    while "GET /api/phone/{pair} 503" not in caplog.text:
        assert time.monotonic() < deadline, caplog.text
        time.sleep(0.01)
    text = caplog.text
    assert "public reaped 1" in text and "public refused api=1 phone=1" in text
    assert "GET /api/state 503" in text
    for secret in (ADDRESS, cookie, tab, generation, pair):
        assert secret not in text
    assert "Traceback" not in text


# --- T-5: the entry point -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["--public-host", "bad host"],
        ["--public-host", "Demo.example"],
        ["--public-host", "demo.example:443"],
        ["--idle-seconds", "60"],
        ["--evict-idle-after", "60"],
        ["--per-ip-sessions", "8"],
        ["--per-ip-new-sessions", "30/600"],
        ["--public-host", "demo.example", "--per-ip-new-sessions", "30"],
        ["--public-host", "demo.example", "--idle-seconds", "100", "--evict-idle-after", "200"],
        ["--public-host", "demo.example", "--max-sessions", "0"],
    ],
)
def test_the_entry_refuses_a_bad_public_host_and_misplaced_public_options(monkeypatch, argv):
    from corridor.web.__main__ import main

    monkeypatch.delenv(public_mode.HOST_ENV, raising=False)
    monkeypatch.delenv(public_mode.DEPLOYMENTS_ENV, raising=False)
    with pytest.raises(SystemExit) as refused:
        main(argv)
    assert refused.value.code == 2


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.5", "demo.example"])
def test_a_public_host_does_not_loosen_the_loopback_check(monkeypatch, host):
    from corridor.web.__main__ import main

    monkeypatch.delenv(public_mode.HOST_ENV, raising=False)
    with pytest.raises(SystemExit) as refused:
        main(["--public-host", "demo.example", "--host", host])
    assert refused.value.code == 2


def test_the_entry_refuses_a_deployment_set_other_than_deepseek_flash(monkeypatch):
    from corridor.web.__main__ import main

    monkeypatch.delenv(public_mode.HOST_ENV, raising=False)
    monkeypatch.setenv(public_mode.DEPLOYMENTS_ENV, "deepseek-flash,zhipu-glm-5.3")
    with pytest.raises(SystemExit) as refused:
        main(["--public-host", "demo.example", "--port", "0"])
    assert refused.value.code == 2


class FakePeriodic:
    made = []

    def __init__(self, callback, every_ms):
        self.callback, self.every_ms, self.running = callback, every_ms, False
        FakePeriodic.made.append(self)

    def start(self):
        self.running = True

    def stop(self):
        self.running = False


def _serve_briefly(args, public, capsys):
    from corridor.web.__main__ import serve

    async def run():
        task = asyncio.ensure_future(serve(args, public, periodic=FakePeriodic))
        await asyncio.sleep(0.3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    FakePeriodic.made.clear()
    asyncio.run(run())
    return capsys.readouterr().out.splitlines()


def test_serve_starts_the_reaper_and_prints_the_public_url_only_in_public_mode(capsys):
    args = SimpleNamespace(host="127.0.0.1", port=0, max_sessions=8)
    lines = _serve_briefly(args, None, capsys)
    assert len(lines) == 2 and FakePeriodic.made == []
    lines = _serve_briefly(args, config(), capsys)
    assert len(lines) == 3 and lines[2] == f"https://{HOST}/"
    (periodic,) = FakePeriodic.made
    assert periodic.every_ms == 60_000 and periodic.running is False  # stopped on the way out
    assert periodic.callback() == 0  # a tick on the stopped app reaps nothing


def test_the_public_entry_point_prints_the_public_url_and_serves_the_public_host_only():
    env = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(("CORRIDOR_WEB_PUBLIC", "CORRIDOR_LIVE", "LLM_", "LIVE_"))
    }
    process = subprocess.Popen(
        [sys.executable, "-m", "corridor.web", "--port", "0", "--public-host", HOST],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        env=env,
    )
    lines = []

    def read_three():
        lines.extend(process.stdout.readline() for _ in range(3))

    reader = threading.Thread(target=read_three, daemon=True)
    reader.start()
    try:
        reader.join(60)
        first, second, third = (line.strip() for line in lines)
        assert re.fullmatch(r"http://127\.0\.0\.1:\d+/", first) and second == first + "?view=judge"
        assert third == f"https://{HOST}/"
        port = int(first.removeprefix("http://").rstrip("/").rsplit(":", 1)[1])
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        connection.request("GET", "/api/state", headers={"Host": HOST})
        answer = connection.getresponse()
        assert answer.status == 401 and json.loads(answer.read())["error"]["code"] == "no_session"
        connection.close()
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        connection.request("GET", "/api/state", headers={"Host": f"127.0.0.1:{port}"})
        answer = connection.getresponse()
        assert answer.status == 403
        assert json.loads(answer.read())["error"]["code"] == "forbidden_origin"
        connection.close()
    finally:
        process.terminate()
        process.wait(10)


# --- T-9: the demo notice (RW-7 item 4; design §4.9) ---------------------------------------------


def test_the_demo_notice_is_on_every_public_page_from_the_first_response(real_public_app):
    from corridor.web import contracts
    from corridor.web.catalog import WEB_MESSAGES

    for view, locale in (("customer", "zh-CN"), ("judge", "en")):
        notice = WEB_MESSAGES["web.vm.public.banner"][locale]
        phone_notice = WEB_MESSAGES["web.vm.public.phone_banner"][locale]
        client = browser(real_public_app)
        first = client.open(view=view, locale=locale)
        assert first["state"]["shell"]["public_banner"] == notice
        disclosure = WEB_MESSAGES["web.vm.public.disclosure"][locale]
        assert first["state"]["shell"]["public_disclosure"] == disclosure
        assert contracts.problems(first) == []
        assert client.json(client.get("/api/state"))["state"]["shell"]["public_banner"] == notice
        # An error answered with the state carries it as well.
        stale = client.post("/api/session/login", {}, headers={"X-Corridor-Generation": "A" * 12})
        answer = client.json(stale)
        assert stale.code == 409 and answer["error"]["code"] == "stale_generation"
        assert answer["state"]["shell"]["public_banner"] == notice
        # So does the reset's response, and the phone page's data.
        reset = client.json(client.post("/api/session/reset", {}))
        assert reset["state"]["shell"]["public_banner"] == notice
        assert reset["state"]["shell"]["public_disclosure"] == disclosure
        ws = ws_of(real_public_app, client)
        phone = client.json(client.get(f"/api/phone/{ws.pair}?lang={locale}&view={view}"))
        assert phone["banner"] == phone_notice == notice
        assert phone["public_disclosure"] == disclosure
        assert phone["public_details_title"]
        assert contracts.problems(phone, view=view) == []
        for text in (notice, phone_notice):
            assert text.strip()


def test_the_demo_notice_says_what_it_must_in_both_locales():
    from corridor.web.catalog import WEB_MESSAGES

    banner = WEB_MESSAGES["web.vm.public.disclosure"]
    for word in ("合成", "模拟银行", "不是真实银行", "请勿输入", "不调用模型", "口令", "发给模型"):
        assert word in banner["zh-CN"], word
    for word in (
        "synthetic",
        "simulated bank",
        "not a real bank",
        "do not enter",
        "calls no model",
        "access code",
        "to the model",
    ):
        assert word in banner["en"], word
    phone = WEB_MESSAGES["web.vm.public.phone_banner"]
    assert phone["zh-CN"] == "演示环境：合成数据、模拟认证，请勿输入真实信息"
    assert "simulated authentication" in phone["en"]


def test_the_demo_notice_says_what_the_canary_measured():
    """RW-9 item 2, against ``tests/web/test_public_live.py``: only long numbers, emails,
    codes and keys become tokens, a person's name does not; a transfer sends the whole address
    book and typed handles. The old wording ("personal details" hidden, only "the contacts
    it names") claimed less than leaves."""
    from corridor.web.catalog import WEB_MESSAGES

    banner = WEB_MESSAGES["web.vm.public.disclosure"]
    for word in (
        "11 位以上的号码",
        "邮箱",
        "验证码",
        "密钥",
        "换成代号",
        "人名",
        "原样发送",
        "意图标签",
        "整张合成通讯录的代号和别名",
        "类型化句柄",
        "提到的联系人代号和称呼",
        "请求摘要与模型输出",
    ):
        assert word in banner["zh-CN"], word
    for word in (
        "11 or more digits",
        "email addresses",
        "verification codes",
        "keys",
        "replaced by tokens",
        "names",
        "as written",
        "intent label",
        "whole synthetic address book's handles and aliases",
        "typed handles",
        "request digests and the model's output",
    ):
        assert word in banner["en"], word
    assert "隐去个人信息" not in banner["zh-CN"]
    assert "personal details replaced" not in banner["en"]


def test_no_demo_notice_outside_public_mode(st_sentinel):
    with serve({}) as app:
        client = WebClient(app)
        first = client.open()
        assert "public_banner" not in first["state"]["shell"]
        ws = ws_of(app, client)
        phone = client.json(client.get(f"/api/phone/{ws.pair}"))
        assert "banner" not in phone


def test_the_pages_draw_the_notice_where_it_cannot_be_closed():
    from corridor.web import handlers

    stage = (handlers.STATIC / "js" / "views" / "stage.js").read_text(encoding="utf-8")
    sms = (handlers.STATIC / "js" / "views" / "sms.js").read_text(encoding="utf-8")
    css = (handlers.STATIC / "css" / "layout.css").read_text(encoding="utf-8")
    # The main page: the first child of the stage, filled from the shell on every update;
    # the phone page: above the status bar. Neither has a control that hides it (the only
    # way it goes away is an empty text, which only the non-public shell sends).
    assert 'class: "public-banner", role: "status"' in stage
    assert '{ class: "stage" }, banner, disclosure, header,' in stage
    assert "parts.banner.textContent = state.shell.public_banner" in stage
    assert 'class: "public-banner", role: "status"' in sms and "payload.banner" in sms
    assert ".public-banner:empty" in css
    assert 'el("details"' in stage and "state.shell.public_disclosure" in stage
    assert "disclosure.hidden = true" in stage and "hidden: true" not in stage
    assert 'h("details"' in sms and "payload.public_disclosure" in sms
    assert "public-banner" not in stage[stage.index("function closeOverlays") :].split("\n\n")[0]


# --- T-10: no real bank, payment or merchant brand in the web assets (design §4.10) --------------

BANK_AND_PAYMENT_BRANDS = (
    "工商银行",
    "建设银行",
    "农业银行",
    "中国银行",
    "交通银行",
    "招商银行",
    "浦发银行",
    "民生银行",
    "兴业银行",
    "光大银行",
    "中信银行",
    "平安银行",
    "邮储银行",
    "华夏银行",
    "广发银行",
    "浙商银行",
    "微众银行",
    "网商银行",
    "银联",
    "支付宝",
    "微信支付",
    "云闪付",
    "恒生银行",
    "汇丰",
    "渣打",
    "中银",
)
MERCHANT_BRANDS = (
    "美团",
    "淘宝",
    "京东",
    "爱奇艺",
    "腾讯",
    "网易",
    "滴滴",
    "拼多多",
    "抖音",
    "哔哩",
    "优酷",
    "芒果",
)
# Latin names are matched on word boundaries, case-sensitive ("Chase" is not "purchase").
LATIN_BRANDS = (
    "ICBC",
    "CCB",
    "BOC",
    "CMB",
    "HSBC",
    "Citi",
    "Citibank",
    "UnionPay",
    "Alipay",
    "WeChat Pay",
    "Visa",
    "Mastercard",
    "JPMorgan",
    "Chase",
    "Wells Fargo",
    "Barclays",
    "Santander",
    "DBS",
    "Netflix",
    "Spotify",
    "Apple",
    "iCloud",
    "Amazon",
)


def test_no_real_bank_or_payment_brand_in_web_assets():
    from pathlib import Path

    import corridor.ui
    from corridor.web import handlers

    web = Path(handlers.__file__).parent
    files = [
        path
        for path in web.rglob("*")
        if path.suffix in {".py", ".js", ".css", ".html", ".json"}
        and "__pycache__" not in path.parts
    ]
    files += sorted(Path(corridor.ui.__file__).parent.glob("*.json"))
    assert len(files) > 80
    found = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        for word in BANK_AND_PAYMENT_BRANDS + MERCHANT_BRANDS:
            if word in text:
                found.append(f"{path.name}: {word}")
        for word in LATIN_BRANDS:
            if re.search(rf"\b{re.escape(word)}\b", text):
                found.append(f"{path.name}: {word}")
    assert not found, found
